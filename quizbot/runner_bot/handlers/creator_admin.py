"""Bot API versions of creator/admin utility commands."""
from __future__ import annotations

import asyncio
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from quizbot.creator_bot import state
from quizbot.database import QuizRepository, UserRepository, get_db
from quizbot.shared import config
from quizbot.shared.utils import revoke_premium

logger = logging.getLogger(__name__)


def _admin(uid: int) -> bool:
    return uid == config.OWNER_ID or uid in config.ADMIN_IDS


FEATURES_TEXT = (
    "🎯 <b>Shikimori — Quiz Platform</b>\n\n"
    "• Create and edit quiz libraries directly in Telegram\n"
    "• Import Telegram quiz polls, TXT/JSON banks and source documents\n"
    "• Generate questions from passages using saved AI providers\n"
    "• Run standard quizzes and timed group poll quizzes\n"
    "• Schedule quizzes and permanent poll quizzes\n"
    "• Share quizzes through inline mode and deep links\n"
    "• Manage paid access, batches, premium plans and reports\n\n"
    "Use /help to see commands."
)


async def features_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(FEATURES_TEXT, parse_mode=ParseMode.HTML)


async def limit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if message and user:
        await message.reply_text(
            state.rate_limit_status_text(user.id)
            + "\n\nThese limits apply to Bot API creator commands. They are process-local and reset when the service restarts."
        )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user or not _admin(user.id):
        if message:
            await message.reply_text("Owner/admin only.")
        return
    status = await message.reply_text("Fetching platform statistics…")
    try:
        users = await UserRepository(get_db()).stats()
        quizzes = await QuizRepository(get_db()).stats()
        await status.edit_text(
            "📊 <b>Platform statistics</b>\n\n"
            f"Users: <b>{users['total_users']}</b> (premium: <b>{users['premium_users']}</b>)\n"
            f"Quizzes: <b>{quizzes['total_quizzes']}</b>\n"
            f"Paid quizzes: <b>{quizzes['paid_quizzes']}</b>\n"
            f"Free quizzes: <b>{quizzes['free_quizzes']}</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("Platform stats failed")
        await status.edit_text("Could not fetch platform statistics. Check application logs.")


async def testapi_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user or user.id != config.OWNER_ID:
        if message:
            await message.reply_text("Owner only.")
        return
    try:
        await get_db().db.command("ping")
        users = await UserRepository(get_db()).stats()
        quizzes = await QuizRepository(get_db()).stats()
        await message.reply_text(
            "✅ <b>MongoDB connectivity: OK</b>\n\n"
            f"Database: <code>{html.escape(config.MONGODB_DB_NAME)}</code>\n"
            f"Users: {users['total_users']}\nQuizzes: {quizzes['total_quizzes']}",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("Database diagnostic failed")
        await message.reply_text("❌ MongoDB connectivity test failed. Check Render logs.")


async def convertall_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user or not _admin(user.id):
        if message:
            await message.reply_text("Owner/admin only.")
        return
    quizzes = await QuizRepository(get_db()).list_all(limit=100000)
    paid = [quiz for quiz in quizzes if quiz.get("quiz_type") == "paid"]
    status = await message.reply_text(f"Converting {len(paid)} paid quiz(es) to free…")
    for quiz in paid:
        await QuizRepository(get_db()).update_field(quiz["qid"], "quiz_type", "free")
    await status.edit_text(f"✅ Converted {len(paid)} paid quiz(es) to free.")


async def delall_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user or not _admin(user.id):
        if message:
            await message.reply_text("Owner/admin only.")
        return
    await message.reply_text(
        "⚠️ This permanently deletes every quiz. Confirm only if you intend to wipe the entire quiz library.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Delete all quizzes", callback_data=f"cad:delall:{user.id}"),
            InlineKeyboardButton("Cancel", callback_data=f"cad:cancel:{user.id}"),
        ]]),
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    _, action, owner = (query.data or "").split(":", 2)
    if not _admin(user.id) or str(user.id) != owner:
        await query.answer("Not authorized.", show_alert=True)
        return
    if action == "cancel":
        await query.message.edit_text("Cancelled. No quizzes were deleted.")
        await query.answer()
        return
    quizzes = await QuizRepository(get_db()).list_all(limit=100000)
    status = await query.message.edit_text(f"Deleting {len(quizzes)} quiz(es)…")
    for quiz in quizzes:
        await QuizRepository(get_db()).delete(quiz["qid"])
    await status.edit_text(f"✅ Deleted {len(quizzes)} quiz(es).")
    await query.answer()


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user or not _admin(user.id):
        if message:
            await message.reply_text("Owner/admin only.")
        return
    if not context.args:
        await message.reply_text("Usage: /ban QUIZ_ID")
        return
    repo = QuizRepository(get_db())
    quiz = await repo.get(context.args[0].strip())
    if not quiz or not quiz.get("creator_id"):
        await message.reply_text("Quiz not found or has no creator ID.")
        return
    creator_id = int(quiz["creator_id"])
    ban_note = ""
    if config.CHANNEL_ID:
        try:
            await context.bot.ban_chat_member(config.CHANNEL_ID, creator_id)
            ban_note = "Creator banned from the configured channel. "
        except Exception:
            logger.exception("Could not ban creator from configured channel")
            ban_note = "Could not ban creator from the configured channel (check bot admin rights). "
    creator_quizzes = await repo.list_by_creator(creator_id)
    for item in creator_quizzes:
        await repo.delete(item["qid"])
    await message.reply_text(f"🚫 {ban_note}Deleted {len(creator_quizzes)} quiz(es) belonging to creator <code>{creator_id}</code>.", parse_mode=ParseMode.HTML)


async def listquiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not config.BOT_GROUP or message.chat_id != config.BOT_GROUP:
        return
    quizzes = await QuizRepository(get_db()).list_all(limit=200)
    if not quizzes:
        await message.reply_text("No quizzes found.")
        return
    bot = await context.bot.get_me()
    for index, quiz in enumerate(quizzes, 1):
        qid = quiz.get("qid", "")
        title = html.escape(str(quiz.get("quiz_name", "Untitled")))
        text = (
            f"📋 <b>Quiz {index}</b>\nName: {title}\nPlays: {quiz.get('total_participants', 0)}\n"
            f"Timer: {quiz.get('timer', 'N/A')}s\nID: <code>{html.escape(str(qid))}</code>\n"
            f"Type: {html.escape(str(quiz.get('quiz_type', 'free')))}"
        )
        await context.bot.send_message(
            chat_id=message.chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Start", url=f"https://t.me/{bot.username}?start={qid}")]]),
        )
        await asyncio.sleep(0.08)


async def gcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user or not _admin(user.id):
        if message:
            await message.reply_text("Owner/admin only.")
        return
    if not message.reply_to_message:
        await message.reply_text("Reply to the message you want to broadcast, then send /gcast.")
        return
    if state.broadcast.active:
        await message.reply_text("A broadcast is already running. Use /stopcast to stop it.")
        return
    state.broadcast.active = True
    source = message.reply_to_message
    users = await UserRepository(get_db()).get_all(limit=1_000_000)
    sent = failed = 0
    progress = await message.reply_text(f"Broadcast started: 0/{len(users)}")
    try:
        for index, row in enumerate(users, 1):
            if not state.broadcast.active:
                break
            try:
                await context.bot.copy_message(
                    chat_id=row["chat_id"],
                    from_chat_id=source.chat_id,
                    message_id=source.message_id,
                )
                sent += 1
            except Exception:
                failed += 1
            if index % 50 == 0:
                await progress.edit_text(f"Broadcast progress: {sent}/{len(users)} sent · {failed} failed")
                await asyncio.sleep(2.0)
            else:
                await asyncio.sleep(0.05)
    finally:
        state.broadcast.active = False
    await progress.edit_text(f"Broadcast complete: {sent}/{len(users)} sent · {failed} failed" + (" · stopped early" if sent + failed < len(users) else ""))


async def stopcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user or not _admin(user.id):
        if message:
            await message.reply_text("Owner/admin only.")
        return
    if not state.broadcast.active:
        await message.reply_text("No broadcast is running.")
        return
    state.broadcast.active = False
    await message.reply_text("Broadcast stop requested.")


def register(application: Application) -> None:
    for name, callback in (
        ("features", features_command), ("limit", limit_command),
        ("statses", stats_command), ("testapi", testapi_command),
        ("convertall", convertall_command), ("delall", delall_command),
        ("ban", ban_command), ("listquiz", listquiz_command),
        ("gcast", gcast_command), ("stopcast", stopcast_command),
    ):
        application.add_handler(CommandHandler(name, callback))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^cad:"))
