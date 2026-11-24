"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from quizbot.database import AuthChatRepository, QuizRepository, get_db
from quizbot.shared import config

from ..state import channel_poll_tasks, rate_limiter, session_mgr
from ..telegram_utils import safe_send_message

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "\U0001F4D6 <b>Runner Bot — Command Reference</b>\n\n"
    "<b>Playing quizzes</b>\n"
    "/start &lt;quiz_id&gt; [skip] — launch a quiz\n"
    "/skip — skip the current question immediately (quiz owner in private; group admins)\n"
    "/pause, /resume, /stop — control the running quiz\n"
    "/slow, /fast, /normal — adjust the per-question timer\n"
    "/timer <seconds> — set an exact timer for upcoming questions (10–3600)\n"
    "/leaderboard — show a live leaderboard mid-quiz\n\n"
    "<b>Other quiz modes</b>\n"
    "/pollquiz, /quizpoll, /publish &lt;quiz_id&gt; [minutes] [anonymous|public] — timed poll quiz\n"
    "/pollsettings — set your default poll timer, anonymity, shuffling and leaderboard\n"
    "/skip closes the current poll during posting; /pollstop ends the whole run\n"
    "/mix &lt;count&gt; &lt;id1&gt; &lt;id2&gt; ... — combine quizzes\n"
    "/aiquiz &lt;topic&gt; — AI-generated quiz\n"
    "/pdfquiz — reply to a PDF to generate a quiz from it\n\n"
    "<b>Reports &amp; settings</b>\n"
    "/html, /pdf — toggle report generation for this chat\n"
    "/trans &lt;lang&gt; — live question translation\n"
    "/schedule QUIZ_ID HH:MM — regular quiz at an IST time\n"
    "/schedule QUIZ_ID 15m — timed poll quiz after a delay\n"
    "/viewschedule, /myschedules, /cancelschedule — manage schedules\n"
)


async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/help` -- command reference."""
    await safe_send_message(ctx, update.effective_chat.id, HELP_TEXT, parse_mode=ParseMode.HTML)


async def handle_channel_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle `/pollquiz`, `/pollstop` and `/skip` sent as channel posts (these
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
        if text.startswith("/skip"):
            from .poll_quiz import skip_active_pollquiz_question
            _ok, response = await skip_active_pollquiz_question(ctx, chat_id)
            await safe_send_message(ctx, chat_id, response)
            return

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
        if not args or len(args) > 2:
            await safe_send_message(ctx, chat_id, "Usage: /pollquiz QUIZ_ID [minutes] (default: 60)")
            return
        from .poll_quiz import _resolve_auto_close_seconds, MAX_POLLQUIZ_MINUTES
        try:
            duration_seconds = (
                int(args[1]) * 60 if len(args) == 2 else _resolve_auto_close_seconds()
            )
            if len(args) == 2 and not 1 <= int(args[1]) <= MAX_POLLQUIZ_MINUTES:
                raise ValueError
            _resolve_auto_close_seconds(duration_seconds)
        except (TypeError, ValueError):
            await safe_send_message(ctx, chat_id, f"Duration must be 1–{MAX_POLLQUIZ_MINUTES} minutes.")
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

        task = asyncio.create_task(
            run_channel_pollquiz(
                ctx, chat_id, quiz, is_anonymous=True,
                auto_close_seconds=duration_seconds,
            )
        )
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
    # /help is owned by the creator-side adapter so a single-bot deployment
    # never emits duplicate replies from both update clients.
    application.add_handler(MessageHandler(filters.COMMAND & filters.ChatType.CHANNEL, handle_channel_command))
