"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
)

from quizbot.database import AuthChatRepository, QuizRepository, get_db
from quizbot.shared import config
from quizbot.creator_bot.handlers.admin import HELP_PAGES, _HELP_HOME_ROWS

from ..state import channel_poll_tasks, rate_limiter, session_mgr
from ..telegram_utils import safe_send_message

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "\U0001F4D6 <b>Runner Bot — Command Reference</b>\n\n"
    "<b>Playing quizzes</b>\n"
    "/start &lt;quiz_id&gt; [skip] — launch a quiz\n"
    "/pause, /resume, /stop — control the running quiz\n"
    "/slow, /fast, /normal — adjust the per-question timer\n"
    "/timer <seconds> — set an exact timer for upcoming questions (10–3600)\n"
    "/leaderboard — show a live leaderboard mid-quiz\n\n"
    "<b>Other quiz modes</b>\n"
    "/pollquiz &lt;quiz_id&gt;, /pollstop — non-expiring poll mode\n"
    "/mix &lt;count&gt; &lt;id1&gt; &lt;id2&gt; ... — combine quizzes\n"
    "/aiquiz &lt;topic&gt; — AI-generated quiz\n"
    "/pdfquiz — reply to a PDF to generate a quiz from it\n\n"
    "<b>Reports &amp; settings</b>\n"
    "/html, /pdf — toggle report generation for this chat\n"
    "/trans &lt;lang&gt; — live question translation\n"
    "/schedule, /viewschedule, /cancelschedule — schedule a quiz\n"
)


def _is_help_owner(user_id: int) -> bool:
    return user_id == config.OWNER_ID or user_id in config.ADMIN_IDS


def _help_keyboard(page: str, user_id: int) -> InlineKeyboardMarkup:
    """PTB equivalent of the creator adapter's interactive help keyboard.

    Both adapters may receive different updates for the same bot account, so
    this UI must be available on the runner side as well.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if page == "home":
        for row in _HELP_HOME_ROWS:
            rows.append([
                InlineKeyboardButton(label, callback_data=f"hmenu:{target}")
                for label, target in row
            ])
        if _is_help_owner(user_id):
            rows.append([InlineKeyboardButton("🛠 Owner Tools", callback_data="hmenu:owner")])
    else:
        related = {
            "create": [("📚 My Quizzes", "library"), ("🤖 AI & Imports", "ai")],
            "library": [("➕ Create & Manage", "create"), ("🔎 Discover", "discover")],
            "discover": [("📚 My Quizzes", "library"), ("📊 Reports", "reports")],
            "access": [("📚 My Quizzes", "library"), ("⚙️ Account", "account")],
            "player": [("📊 Reports", "reports"), ("🔎 Discover", "discover")],
            "reports": [("🎮 Quiz Player", "player"), ("📚 My Quizzes", "library")],
            "ai": [("➕ Create & Manage", "create"), ("⚙️ Account", "account")],
            "account": [("✨ Features", "features"), ("📦 Batches", "access")],
            "features": [("➕ Create & Manage", "create"), ("🎮 Quiz Player", "player")],
            "owner": [("📊 Reports", "reports"), ("⚙️ Account", "account")],
        }
        destinations = related.get(page, [])
        if destinations:
            rows.append([
                InlineKeyboardButton(label, callback_data=f"hmenu:{target}")
                for label, target in destinations
            ])
    rows.append([
        InlineKeyboardButton("⌂ Main Menu", callback_data="hmenu:home"),
        InlineKeyboardButton("✖ Close", callback_data="hmenu:close"),
    ])
    return InlineKeyboardMarkup(rows)


async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/help` -- open the modern Quizbot control centre.

    This duplicates the creator-side entry point intentionally: Telegram may
    deliver an update to either active bot session, and the runner used to
    ignore /help entirely.
    """
    message = update.effective_message
    user = update.effective_user
    if not message:
        return
    await message.reply_text(
        HELP_PAGES["home"],
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_help_keyboard("home", user.id if user else 0),
        disable_web_page_preview=True,
    )


async def features_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/features` -- open the interactive feature overview."""
    message = update.effective_message
    user = update.effective_user
    if not message:
        return
    await message.reply_text(
        HELP_PAGES["features"],
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_help_keyboard("features", user.id if user else 0),
        disable_web_page_preview=True,
    )


async def help_menu_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle hmenu callbacks if Telegram routes them to the runner session."""
    query = update.callback_query
    if not query:
        return
    page = (query.data or "").partition(":")[2]

    if page == "close":
        try:
            if query.message:
                await query.message.delete()
        finally:
            await query.answer("Menu closed.")
        return

    if page not in HELP_PAGES:
        await query.answer("This menu button has expired. Send /help to reopen it.", show_alert=True)
        return
    if page == "owner" and not _is_help_owner(query.from_user.id):
        await query.answer("Owner/admin access only.", show_alert=True)
        return
    if not query.message:
        await query.answer()
        return

    try:
        await query.edit_message_text(
            HELP_PAGES[page],
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_help_keyboard(page, query.from_user.id),
            disable_web_page_preview=True,
        )
    except Exception as exc:
        # Ignore harmless rapid double-taps / Message is not modified errors.
        logger.debug("Runner help-menu edit failed for page=%s: %s", page, exc)
    await query.answer()


async def handle_channel_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle `/pollquiz` and `/pollstop` sent as channel posts (these
    arrive via `update.channel_post`, not `update.message`)."""
    from .poll_quiz import run_channel_pollquiz

    msg = update.channel_post
    if not msg:
        return
    text = (msg.text or "").strip()
    chat_id = msg.chat_id

    try:
        await ctx.bot.delete_message(chat_id, msg.message_id)
    except Exception:
        pass

    try:
        if text.startswith("/pollstop"):
            task = channel_poll_tasks.get(chat_id)
            if task and not task.done():
                task.cancel()
            else:
                await safe_send_message(ctx, chat_id, "⚠️ No poll quiz is running.")
            return

        if not text.startswith("/pollquiz"):
            return

        args = text.split()[1:]
        if not args:
            await safe_send_message(ctx, chat_id, "Usage: /pollquiz QUIZID")
            return

        existing = channel_poll_tasks.get(chat_id)
        if existing and not existing.done():
            await safe_send_message(ctx, chat_id, "⚠️ A poll quiz is already running. Use /pollstop to stop it.")
            return

        qid = args[0]
        quiz_repo = QuizRepository(get_db())
        quiz = await quiz_repo.get(qid)
        if not quiz:
            await safe_send_message(ctx, chat_id, "❌ Invalid QuestionSetID.")
            return

        creator_id = quiz.get("creator_id")
        if quiz.get("quiz_type") == "paid" and creator_id:
            auth_repo = AuthChatRepository(get_db())
            auth_users = await auth_repo.get(creator_id)
            if chat_id not in auth_users:
                try:
                    cinfo = await ctx.bot.get_chat(creator_id)
                    details = f"\U0001F464 {cinfo.first_name or ''}\n\U0001F4AC @{cinfo.username or 'N/A'}\n\U0001F522 ID: {cinfo.id}"
                    await safe_send_message(ctx, chat_id, f"❌ This is a paid quiz. Contact creator for access.\n\n{details}")
                except Exception:
                    await safe_send_message(ctx, chat_id, f"❌ Paid quiz. Contact creator ID {creator_id} for access.")
                return

        questions = quiz.get("questions", [])
        if not questions:
            await safe_send_message(ctx, chat_id, "❌ Quiz has no questions.")
            return

        task = asyncio.create_task(run_channel_pollquiz(ctx, chat_id, quiz, is_anonymous=True))
        channel_poll_tasks[chat_id] = task
    except Exception as e:
        logger.error("handle_channel_command error: %s", e, exc_info=True)


async def watchdog_loop() -> None:
    """Periodically clean up expired quiz sessions and rate-limiter buckets."""
    while True:
        try:
            await asyncio.sleep(config.WATCHDOG_INTERVAL)
            await session_mgr.cleanup()
            rate_limiter.cleanup()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Watchdog error: %s", e)


def register(application: Application) -> None:
    # Register the help UI here as well as in the creator adapter. Telegram
    # distributes updates among the bot's active sessions, so either adapter
    # may receive /help or an hmenu callback.
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("features", features_command))
    application.add_handler(CallbackQueryHandler(help_menu_callback, pattern=r"^hmenu:"))
    application.add_handler(MessageHandler(filters.COMMAND & filters.ChatType.CHANNEL, handle_channel_command))
