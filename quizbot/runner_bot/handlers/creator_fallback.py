"""Bot API fallback for essential creator commands.

The main creator adapter uses MTProto/Kurigram. If Telegram's MTProto bot
authorization endpoint is temporarily unavailable, this module keeps the
basic /create -> add questions -> /done workflow usable through the already
running python-telegram-bot adapter.

When the MTProto adapter is healthy, these handlers stand aside so the full
creator implementation remains the source of truth.
"""
from __future__ import annotations

import logging
import secrets
import string
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from quizbot.creator_bot import state as creator_state
from quizbot.creator_bot.parsing import (
    filter_words,
    parse_question_block,
    split_question_blocks,
    strip_source_noise,
)
from quizbot.database import CreatorSettingsRepository, QuizRepository, UserRepository, get_db
from quizbot.shared import config, runtime
from quizbot.shared.utils import is_premium_user

logger = logging.getLogger(__name__)

MIN_QUESTIONS = 10
MAX_QUESTIONS = 300
MAX_QUESTIONS_OWNER = 3000

# Commands handled by the MTProto creator adapter. While it is disconnected,
# answer these explicitly rather than leaving users with no response.
_CREATOR_ONLY_COMMANDS = (
    "sourcequiz", "createfromtext", "sourcequizcancel", "myquizzes", "edit",
    "info", "del", "delete", "add", "rem", "remall", "setpromo", "search",
    "quiz", "auth", "pay", "setkey", "mykeys", "delkey", "settings", "batch",
    "createbatch", "searchbatch", "stopedit", "whtml", "testseries", "tsr",
    "mocktest", "gcast", "stopcast", "statses", "testapi", "leaders",
    "aspirants", "limit", "listquiz", "removeuser", "remove", "clearlist",
    "mywords",
)


def _new_qid() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "GGN" + "".join(secrets.choice(alphabet) for _ in range(6))


def _is_fallback_session(user_id: int) -> bool:
    session = creator_state.quiz_creation.get(user_id)
    return bool(session and session.get("_bot_api_fallback"))


async def create_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if runtime.CREATOR_ADAPTER_READY:
        return
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if update.effective_chat and update.effective_chat.type != "private":
        await message.reply_text("Please create quizzes in a private chat with me.")
        return
    if not await is_premium_user(user.id):
        await message.reply_text("🔒 Quiz creation requires premium access. Use /pay to see available plans.")
        return
    if user.id in creator_state.quiz_creation:
        await message.reply_text("⚠️ You already have a quiz-creation session. Use /done or /cancel.")
        return

    creator_state.quiz_creation[user.id] = {
        "_bot_api_fallback": True,
        "questions": [],
        "timer": 20,
        "quiz_name": None,
        "awaiting_name": True,
    }
    await message.reply_text(
        "📝 Send the quiz name.\n\n"
        "Then send questions in the usual format, mark the correct option with ✅, "
        "and use /done when you have added at least 10 questions.\n"
        "Use /cancel to discard the draft."
    )


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if runtime.CREATOR_ADAPTER_READY:
        return
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    session = creator_state.quiz_creation.get(user.id)
    if not session or not session.get("_bot_api_fallback"):
        await message.reply_text("⚠️ Start with /create first.")
        return
    if not await is_premium_user(user.id):
        await message.reply_text("🔒 Quiz creation requires premium access. Use /pay.")
        return

    count = len(session.get("questions", []))
    max_allowed = MAX_QUESTIONS_OWNER if user.id == config.OWNER_ID else MAX_QUESTIONS
    if count < MIN_QUESTIONS:
        await message.reply_text(f"⚠️ You need at least {MIN_QUESTIONS} questions. You currently have {count}.")
        return
    if count > max_allowed:
        await message.reply_text(f"⚠️ Maximum allowed is {max_allowed} questions. You have {count}.")
        return

    session["awaiting_type"] = True
    await message.reply_text("📊 Choose quiz type: send `free` or `paid`.", parse_mode="Markdown")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if runtime.CREATOR_ADAPTER_READY:
        return
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    session = creator_state.quiz_creation.get(user.id)
    if session and session.get("_bot_api_fallback"):
        creator_state.quiz_creation.pop(user.id, None)
        await message.reply_text("❌ Quiz draft cancelled.")
    else:
        await message.reply_text("There is no active quiz draft to cancel.")


async def _save_fallback_quiz(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    quiz_type: str,
) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    session = creator_state.quiz_creation.get(user.id)
    if not session or not session.get("_bot_api_fallback"):
        return

    settings = await CreatorSettingsRepository(get_db()).get(user.id)
    default_text = settings.get("default_text")
    default_text_field = settings.get("default_text_field", "both")
    questions = session.get("questions", [])
    if default_text:
        for question in questions:
            if default_text_field in ("question", "both") and question.get("question"):
                question["question"] = question["question"].rstrip() + "\n" + default_text
            if default_text_field in ("explanation", "both"):
                existing = (question.get("explanation") or "").rstrip()
                question["explanation"] = f"{existing}\n{default_text}" if existing else default_text

    qid = _new_qid()
    quiz_name = session.get("quiz_name") or "Untitled quiz"
    quiz = await QuizRepository(get_db()).create(
        creator_id=user.id,
        quiz_name=quiz_name,
        questions=questions,
        qid=qid,
        sections=[],
        timer=int(session.get("timer") or 20),
        quiz_type=quiz_type,
        negative_marks=0,
        promo_message=None,
    )
    creator_state.quiz_creation.pop(user.id, None)

    if not quiz:
        await message.reply_text("⚠️ Quiz could not be loaded after saving. Please check /myquizzes when creator tools reconnect.")
        return

    bot = await context.bot.get_me()
    bot_username = bot.username
    rows = []
    if bot_username:
        rows.append([InlineKeyboardButton("🚀 Start quiz", url=f"https://t.me/{bot_username}?start={qid}")])
        rows.append([InlineKeyboardButton("👥 Add to group", url=f"https://t.me/{bot_username}?startgroup={qid}")])
    rows.append([InlineKeyboardButton("🔗 Share quiz", switch_inline_query=qid)])
    keyboard = InlineKeyboardMarkup(rows)

    await message.reply_text(
        "🎉 Quiz created!\n\n"
        f"📝 Name: {quiz_name}\n"
        f"❓ Questions: {len(questions)}\n"
        f"⏱ Timer: {session.get('timer') or 20}s\n"
        f"🆔 Quiz ID: {qid}\n"
        f"🏷 Type: {quiz_type}\n\n"
        "You can now use /start QUIZ_ID or /pollquiz QUIZ_ID.",
        reply_markup=keyboard,
    )

    if config.BOT_GROUP:
        try:
            await context.bot.send_message(
                chat_id=config.BOT_GROUP,
                text=f"🎉 New quiz created: {quiz_name}\nQuestions: {len(questions)}\nQuiz ID: {qid}",
                reply_markup=keyboard,
            )
        except Exception:
            logger.debug("Could not announce fallback-created quiz in BOT_GROUP", exc_info=True)


async def handle_creation_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if runtime.CREATOR_ADAPTER_READY:
        return
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not _is_fallback_session(user.id):
        return
    session = creator_state.quiz_creation[user.id]

    if message.poll:
        poll = message.poll
        if poll.type != "quiz":
            await message.reply_text("⚠️ Only Telegram quiz polls can be imported. Please forward a quiz-type poll.")
            return
        user_doc = await UserRepository(get_db()).get_or_create(user.id)
        remove_words = user_doc.get("remove_words", [])
        options = [filter_words(option.text, remove_words) or "" for option in poll.options]
        correct_id = poll.correct_option_id
        if correct_id is None or not (0 <= correct_id < len(options)):
            await message.reply_text("⚠️ This poll does not expose a valid correct answer. Please paste the question manually.")
            return
        session["questions"].append(
            {
                "question": strip_source_noise(filter_words(poll.question, remove_words)),
                "options": options,
                "correct_option_id": correct_id,
                "explanation": poll.explanation,
                "file_id": None,
                "reply_text": None,
            }
        )
        await message.reply_text(f"✅ Imported. Total questions: {len(session['questions'])}. Send more or /done.")
        return

    if message.document:
        document = message.document
        filename = document.file_name or "questions.txt"
        if not filename.lower().endswith((".txt", ".json")):
            await message.reply_text("⚠️ The basic creator fallback supports .txt and .json files only.")
            return
        try:
            telegram_file = await context.bot.get_file(document.file_id)
            content = bytes(await telegram_file.download_as_bytearray())
            user_doc = await UserRepository(get_db()).get_or_create(user.id)
            from quizbot.creator_bot.handlers.file_import import process_uploaded_file

            count, error = process_uploaded_file(
                content, filename, session["questions"], user_doc.get("remove_words", [])
            )
            if error:
                await message.reply_text(f"❌ Import failed: {error}")
            else:
                await message.reply_text(f"✅ Imported {count} questions. Total: {len(session['questions'])}. Send more or /done.")
        except Exception as exc:
            logger.exception("Bot API creator fallback document import failed")
            await message.reply_text(f"❌ Could not import that file: {type(exc).__name__}.")
        return

    text = message.text or ""
    if not text:
        return

    if session.get("awaiting_name"):
        name = text.strip()
        if not name:
            await message.reply_text("⚠️ Please send a non-empty quiz name.")
            return
        session["quiz_name"] = name
        session["awaiting_name"] = False
        await message.reply_text(
            f"📝 Quiz name: {name}\n\n"
            "Now send questions. Separate questions with a blank line and mark the correct option with ✅.\n"
            "Example:\n\nWhat is 2 + 2?\nA) 3\nB) 4 ✅\nC) 5\nD) 6\n\n"
            "Send at least 10 questions, then use /done."
        )
        return

    if session.get("awaiting_type"):
        quiz_type = text.strip().lower()
        if quiz_type not in {"free", "paid"}:
            await message.reply_text("⚠️ Please send exactly `free` or `paid`.", parse_mode="Markdown")
            return
        await _save_fallback_quiz(update, context, quiz_type)
        return

    user_doc = await UserRepository(get_db()).get_or_create(user.id)
    remove_words = user_doc.get("remove_words", [])
    blocks = split_question_blocks(text)
    parsed_questions: list[dict[str, Any]] = []
    for block in blocks:
        if not block.strip():
            continue
        parsed = parse_question_block(block)
        if not parsed:
            await message.reply_text(
                "⚠️ I couldn't parse that question. Mark the correct option with ✅ and use 2–10 options.\n"
                "Separate complete questions with a blank line."
            )
            return
        parsed["question"] = strip_source_noise(filter_words(parsed.get("question"), remove_words))
        parsed["options"] = [filter_words(option, remove_words) or "" for option in parsed.get("options", [])]
        parsed_questions.append(parsed)

    if not parsed_questions:
        await message.reply_text("⚠️ No valid questions found in that message.")
        return
    session["questions"].extend(parsed_questions)
    total = len(session["questions"])
    await message.reply_text(f"✅ Added {len(parsed_questions)} question(s). Total: {total}. Send more or /done.")


async def creator_unavailable_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if runtime.CREATOR_ADAPTER_READY:
        return
    message = update.effective_message
    if message:
        await message.reply_text(
            "⚠️ This creator feature is temporarily unavailable because the creator connection "
            "has not completed Telegram MTProto authorization.\n\n"
            "Quiz playback and timed poll features remain available. Basic quiz creation is "
            "available through /create, /done and /cancel. Check /creatorstatus for connection status."
        )


def register(application: Application) -> None:
    application.add_handler(CommandHandler("create", create_command))
    application.add_handler(CommandHandler("done", done_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("creatorstatus", creator_status_command))
    for command in _CREATOR_ONLY_COMMANDS:
        application.add_handler(CommandHandler(command, creator_unavailable_command))
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (filters.TEXT | filters.POLL) & ~filters.COMMAND,
            handle_creation_message,
        ),
        group=-1,
    )
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.Document.ALL, handle_creation_message),
        group=-1,
    )


async def creator_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    if runtime.CREATOR_ADAPTER_READY:
        await message.reply_text("✅ Creator adapter is connected. Full creator features are available.")
    else:
        await message.reply_text(
            "⚠️ Creator adapter is currently disconnected and retrying Telegram MTProto authorization.\n\n"
            "✅ Quiz playback and timed polls use the Bot API and remain available.\n"
            "✅ Basic /create, /done and /cancel are available through the Bot API fallback.\n"
            "⏳ Advanced creator tools will reconnect when MTProto authorization succeeds."
        )
