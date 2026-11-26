"""Shikimori dashboard, help and channel-post command routing."""
from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

from quizbot.database import AuthChatRepository, QuizRepository, get_db
from quizbot.shared import config
from ..state import channel_poll_tasks, rate_limiter, session_mgr
from ..telegram_utils import safe_send_message

logger = logging.getLogger(__name__)

WELCOME_TEXT = (
    "🌸 <b>Shikimori</b> — your quiz studio\n\n"
    "Hi! I'm Shikimori. I can help you create quizzes, publish permanent "
    "forwardable polls, manage your question library and track results.\n\n"
    "Choose what you'd like to do below, or use /help to see every command."
)

HELP_TEXT = (
    "🌸 <b>Shikimori — Command Guide</b>\n\n"
    "<b>Quiz player</b>\n"
    "/start &lt;quiz_id&gt; — open a quiz\n"
    "/skip — skip the current question\n"
    "/pause, /resume, /stop — control a running quiz\n"
    "/slow, /fast, /normal — adjust quiz speed\n"
    "/timer &lt;seconds&gt; — set a quiz timer\n"
    "/leaderboard — show live standings\n\n"
    "<b>Permanent poll quizzes</b>\n"
    "/pollquiz &lt;quiz_id&gt; [anonymous|public] — publish forwardable polls\n"
    "/pollsettings — anonymity, shuffling and leaderboard preferences\n"
    "/skip — skip the current poll while publishing\n"
    "/pollstop — stop publishing; polls already sent stay open\n"
    "/pollclose — close sent polls without showing a leaderboard\n"
    "/pollfinish — close polls and publish final results\n\n"
    "<b>Create &amp; manage</b>\n"
    "/create — create a quiz\n"
    "/done, /cancel — save or discard a draft\n"
    "/sourcequiz [1–20] — generate questions from source material\n"
    "/myquizzes [search], /info &lt;id&gt;, /search &lt;term&gt;\n"
    "/edit &lt;id&gt;, /del &lt;id&gt; — edit or delete a quiz\n"
    "/batch, /createbatch, /searchbatch — batch tools\n"
    "/mix, /aiquiz, /pdfquiz — additional quiz tools\n\n"
    "<b>Settings &amp; reports</b>\n"
    "/settings, /pollsettings, /setkey, /mykeys\n"
    "/html, /pdf, /trans &lt;language&gt;\n"
    "/schedule &lt;quiz_id&gt; HH:MM — schedule a regular quiz\n"
    "/myschedules, /cancelschedule — manage schedules\n"
    "/pay — premium plans\n"
    "/creatorstatus — connection status\n\n"
    "<b>Quick start</b>\n"
    "1. Create a quiz with /create.\n"
    "2. Save it and copy its Quiz ID.\n"
    "3. Publish it with /pollquiz QUIZ_ID.\n"
    "4. Use /pollstop, /pollclose or /pollfinish depending on what you need."
)


def dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✨ Create quiz", callback_data="shk:create"),
         InlineKeyboardButton("📚 My library", callback_data="shk:library")],
        [InlineKeyboardButton("📣 Publish polls", callback_data="shk:polls"),
         InlineKeyboardButton("⚙️ Poll settings", callback_data="shk:pollsettings")],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="shk:leaderboard"),
         InlineKeyboardButton("❔ Help & guide", callback_data="shk:help")],
        [InlineKeyboardButton("🔎 Creator status", callback_data="shk:status"),
         InlineKeyboardButton("🏠 Home", callback_data="shk:home")],
    ])


async def dashboard_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message:
        await message.reply_text(WELCOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=dashboard_keyboard())


async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message:
        await message.reply_text(
            HELP_TEXT, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Shikimori home", callback_data="shk:home")]]),
        )


async def dashboard_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    action = (query.data or "").split(":", 1)[-1]

    if action == "home":
        await query.edit_message_text(WELCOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=dashboard_keyboard())
        return
    if action == "help":
        await query.edit_message_text(HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=dashboard_keyboard())
        return

    # Feature handlers are imported lazily to keep module registration acyclic.
    if action == "create":
        from .creator import create_command
        await create_command(update, ctx)
    elif action == "library":
        from .creator import myquizzes_command
        await myquizzes_command(update, ctx)
    elif action == "pollsettings":
        from .poll_settings import pollsettings_command
        await pollsettings_command(update, ctx)
    elif action == "polls":
        await query.message.reply_text(
            "📣 <b>Permanent poll publishing</b>\n\n"
            "Publish a saved quiz with:\n<code>/pollquiz QUIZ_ID</code>\n\n"
            "Polls are forwardable and have no expiry timer.\n"
            "• /pollstop — stop sending new polls; sent polls stay open.\n"
            "• /pollclose — close sent polls without a leaderboard.\n"
            "• /pollfinish — close polls and publish final results.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Poll settings", callback_data="shk:pollsettings")], [InlineKeyboardButton("🏠 Home", callback_data="shk:home")]]),
        )
    elif action == "leaderboard":
        await query.message.reply_text("🏆 Use /leaderboard in the quiz group to view live standings, or /leaderboard QUIZ_ID for saved results.")
    elif action == "status":
        from .creator import creator_status_command
        await creator_status_command(update, ctx)


async def handle_channel_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Route channel-post commands using the same permanent-poll semantics."""
    from .poll_quiz import run_channel_pollquiz
    msg = update.channel_post
    if not msg:
        return
    text = (msg.text or "").strip()
    if not text.startswith("/"):
        return
    command = text.split()[0].split("@", 1)[0].lower()
    chat_id = msg.chat_id

    try:
        await ctx.bot.delete_message(chat_id, msg.message_id)
    except Exception:
        pass

    try:
        if command == "/skip":
            from .poll_quiz import skip_active_pollquiz_question
            _ok, response = await skip_active_pollquiz_question(ctx, chat_id)
            await safe_send_message(ctx, chat_id, response)
            return
        if command == "/pollstop":
            from .poll_quiz import _stop_poll_publisher
            cancelled, posted = await _stop_poll_publisher(ctx, chat_id)
            await safe_send_message(ctx, chat_id, f"⏸ Poll publishing stopped. {posted} sent poll(s) remain open. Use /pollclose or /pollfinish when ready.")
            return
        if command in {"/pollclose", "/pollfinish"}:
            from .poll_quiz import pollclose_command, pollfinish_command
            if command == "/pollclose":
                await pollclose_command(update, ctx)
            else:
                await pollfinish_command(update, ctx)
            return
        if command not in {"/pollquiz", "/quizpoll", "/publish"}:
            return

        args = text.split()[1:]
        if not args or len(args) > 2:
            await safe_send_message(ctx, chat_id, "Usage: /pollquiz QUIZ_ID. Polls are permanent and forwardable.")
            return
        qid = args[0]
        is_anonymous = True
        if len(args) == 2 and args[1].lower() in {"public", "scored"}:
            await safe_send_message(ctx, chat_id, "Channel polls are anonymous by Telegram design; public scoring is available in groups.")
            return
        if len(args) == 2 and args[1].lower() not in {"anonymous", "anon"}:
            await safe_send_message(ctx, chat_id, "Timer arguments are no longer supported. Use /pollquiz QUIZ_ID.")
            return

        existing = channel_poll_tasks.get(chat_id)
        if existing and not existing.done():
            await safe_send_message(ctx, chat_id, "⚠️ A poll quiz is already being published here.")
            return
        quiz = await QuizRepository(get_db()).get(qid)
        if not quiz:
            await safe_send_message(ctx, chat_id, "❌ Quiz ID not found.")
            return
        if quiz.get("quiz_type") == "paid" and quiz.get("creator_id"):
            allowed = await AuthChatRepository(get_db()).get(quiz["creator_id"])
            if chat_id not in allowed:
                await safe_send_message(ctx, chat_id, "🔒 This paid quiz is not authorised for this channel.")
                return
        task = asyncio.create_task(run_channel_pollquiz(ctx, chat_id, quiz, is_anonymous=is_anonymous))
        channel_poll_tasks[chat_id] = task
    except Exception:
        logger.exception("Channel command routing failed for %s", command)


async def watchdog_loop() -> None:
    while True:
        try:
            await asyncio.sleep(config.WATCHDOG_INTERVAL)
            await session_mgr.cleanup()
            rate_limiter.cleanup()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Watchdog error")


def register(application: Application) -> None:
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler(["menu", "home"], dashboard_command))
    application.add_handler(CallbackQueryHandler(dashboard_callback, pattern=r"^shk:"))
    application.add_handler(MessageHandler(filters.COMMAND & filters.ChatType.CHANNEL, handle_channel_command))
