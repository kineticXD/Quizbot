"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from quizbot.database import PollQuizSettingsRepository, ScheduledQuizRepository, QuizRepository, get_db
from quizbot.shared.utils import is_premium_user
from ..schedule_utils import parse_schedule_spec
from ..quiz_utils import resolve_quiz_access

from ..telegram_utils import safe_send_message

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")


class ScheduledQuizManager:
    """MongoDB-backed schedule manager; pending jobs are restored on startup."""

    def __init__(self, scheduler: AsyncIOScheduler, application) -> None:
        self.scheduler = scheduler
        self.application = application
        self.repo = ScheduledQuizRepository(get_db())
        self.jobs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def _job_time(self, run_at: float) -> datetime:
        scheduled = datetime.fromtimestamp(float(run_at), tz=timezone.utc).astimezone(IST)
        now = datetime.now(IST)
        # A schedule missed during downtime should run promptly after recovery.
        return max(scheduled, now + timedelta(seconds=2))

    def _register_job(self, record: dict[str, Any]) -> None:
        schedule_id = str(record["schedule_id"])
        run_date = self._job_time(float(record["run_at"]))
        self.scheduler.add_job(
            self._run, trigger=DateTrigger(run_date=run_date),
            args=[schedule_id], id=schedule_id, replace_existing=True,
            misfire_grace_time=86400,
        )
        self.jobs[schedule_id] = record

    async def restore(self) -> int:
        """Rebuild APScheduler jobs from MongoDB after each process restart."""
        records = await self.repo.list_pending()
        restored = 0
        async with self._lock:
            for record in records:
                try:
                    self._register_job(record)
                    restored += 1
                except Exception:
                    logger.exception("Could not restore scheduled quiz %s", record.get("schedule_id"))
        logger.info("Restored %d pending scheduled quiz(es)", restored)
        return restored

    async def add(
        self, chat_id: int, qid: str, scheduled_time: datetime, created_by: int,
        ctx: Optional[ContextTypes.DEFAULT_TYPE] = None, thread_id: Optional[int] = None,
        run_mode: str = "quiz", poll_settings: Optional[dict] = None,
    ) -> str:
        schedule_id = "qsch_" + uuid.uuid4().hex[:12]
        record = await self.repo.create(
            schedule_id=schedule_id, chat_id=chat_id, quiz_id=qid,
            run_at=scheduled_time.timestamp(), created_by=created_by, thread_id=thread_id,
            run_mode=run_mode, poll_settings=poll_settings,
        )
        async with self._lock:
            try:
                self._register_job(record)
            except Exception:
                await self.repo.delete(schedule_id)
                raise
        return schedule_id

    async def remove(self, job_id: str) -> bool:
        async with self._lock:
            existed = self.jobs.pop(job_id, None) is not None
            try:
                self.scheduler.remove_job(job_id)
            except Exception:
                pass
            deleted = await self.repo.delete(job_id)
            return existed or deleted

    async def get_for_chat(self, chat_id: int) -> list[dict[str, Any]]:
        records = await self.repo.list_for_chat(chat_id)
        output = []
        for record in records:
            record["job_id"] = record["schedule_id"]
            record["scheduled_time"] = datetime.fromtimestamp(float(record["run_at"]), tz=timezone.utc).astimezone(IST)
            output.append(record)
        return output

    async def _run(self, schedule_id: str) -> None:
        """Consume a persisted job once, then launch its standard or poll runner."""
        record = await self.repo.get(schedule_id)
        if not record:
            self.jobs.pop(schedule_id, None)
            return
        # Remove before delivery for at-most-once behaviour after process crashes.
        await self.repo.delete(schedule_id)
        self.jobs.pop(schedule_id, None)
        chat_id = int(record["chat_id"])
        qid = str(record["quiz_id"])
        admin_id = int(record.get("created_by") or chat_id)
        thread_id = record.get("thread_id")
        ctx = SimpleNamespace(bot=self.application.bot, application=self.application)
        try:
            quiz_repo = QuizRepository(get_db())
            quiz = await quiz_repo.get(qid)
            if not quiz:
                await safe_send_message(ctx, chat_id, f"❌ Scheduled quiz {qid} not found. Try /start {qid} manually.", thread_id=thread_id)
                return

            run_at = datetime.fromtimestamp(float(record["run_at"]), tz=timezone.utc).astimezone(IST)
            if record.get("run_mode") == "poll":
                from .poll_quiz import run_channel_pollquiz
                from ..state import channel_poll_tasks, pollquiz_sessions, session_mgr

                current_task = channel_poll_tasks.get(chat_id)
                previous_poll_session = pollquiz_sessions.get(chat_id)
                if (current_task and not current_task.done()) or session_mgr.get(chat_id) or (previous_poll_session and not previous_poll_session.get("closed") and not previous_poll_session.get("finalized")):
                    await safe_send_message(
                        ctx, chat_id,
                        f"⚠️ Scheduled poll quiz {qid} was not started because another quiz is active in this chat.",
                        thread_id=thread_id,
                    )
                    return

                poll_settings = record.get("poll_settings") or {}
                task = asyncio.create_task(run_channel_pollquiz(
                    ctx, chat_id, quiz,
                    is_anonymous=bool(poll_settings.get("anonymous", False)),
                    thread_id=thread_id,
                    shuffle_questions=bool(poll_settings.get("shuffle_questions", False)),
                    shuffle_options=bool(poll_settings.get("shuffle_options", False)),
                    publish_scoreboard=bool(poll_settings.get("scoreboard", True)),
                ))
                channel_poll_tasks[chat_id] = task
                await safe_send_message(
                    ctx, chat_id,
                    f"📣 <b>Scheduled persistent poll quiz is starting!</b>\n\n"
                    f"Quiz: <b>{quiz.get('quiz_name', 'Quiz')}</b> (<code>{qid}</code>)\n"
                    "Polls have no expiry timer. Use /pollstop to stop further publishing, /pollclose to close polls, or /pollfinish to close them and publish final results.",
                    parse_mode=ParseMode.HTML, thread_id=thread_id,
                )
                return

            from .setup_wizard import _launch_quiz_from_settings
            quiz["question_set_id"] = quiz["qid"]
            quiz["negative_marking"] = quiz.get("negative_marks", 0)
            quiz["correct_mark"] = quiz.get("correct_marks", 1)

            class _FakeUser:
                def __init__(self, uid: int) -> None:
                    self.id = uid
                    self.first_name = "Scheduled"
                    self.is_bot = False

            class _FakeChat:
                def __init__(self, cid: int) -> None:
                    self.id = cid
                    self.type = "group"

            class _FakeMessage:
                def __init__(self, cid: int) -> None:
                    self.chat_id = cid
                    self.chat = _FakeChat(cid)
                    self.from_user = _FakeUser(admin_id)
                    self.message_id = int(time.time())
                    self.message_thread_id = thread_id

            class _FakeUpdate:
                def __init__(self, cid: int) -> None:
                    self.message = _FakeMessage(cid)
                    self.update_id = int(time.time())

            ps = {
                "quiz": quiz, "skip": 0, "protect": False, "chat_type": "group",
                "correct_mark": 1.0, "neg_mark": quiz.get("negative_marking", 0.0),
                "shuffle_q": bool(quiz.get("shuffle_questions", False)),
                "shuffle_o": bool(quiz.get("shuffle_options", False)),
                "timer_override": None, "initiator_id": admin_id,
                "update": _FakeUpdate(chat_id),
            }

            await safe_send_message(
                ctx, chat_id,
                f"🎯 <b>Scheduled Quiz Starting!</b>\n\nQuiz ID: <code>{qid}</code>\n"
                f"Scheduled for: {run_at.strftime('%I:%M %p, %d %b')}\n"
                f"⏱ Timer: {quiz.get('timer', 30)}s | Questions: {len(quiz.get('questions', []))}",
                parse_mode=ParseMode.HTML, thread_id=thread_id,
            )
            await _launch_quiz_from_settings(chat_id, ctx, ps)
        except Exception as e:
            logger.error("Scheduled quiz exec error: %s", e, exc_info=True)
            try:
                await safe_send_message(ctx, chat_id, f"❌ Error starting scheduled quiz {qid}: {e}\nTry /start {qid}", thread_id=thread_id)
            except Exception:
                pass


schedule_mgr: "ScheduledQuizManager | None" = None


def init_schedule_manager(scheduler: AsyncIOScheduler, application) -> ScheduledQuizManager:
    """Called once from bot.py after the shared scheduler is created."""
    global schedule_mgr
    schedule_mgr = ScheduledQuizManager(scheduler, application)
    return schedule_mgr


async def schedule_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/schedule ID HH:MM` for a regular quiz or `/schedule ID 15m` for a persistent poll."""
    chat_id = update.message.chat_id
    try:
        user_id = update.message.from_user.id
        if update.message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            await safe_send_message(ctx, chat_id, "❌ Groups and supergroups only.")
            return

        try:
            member = await ctx.bot.get_chat_member(chat_id, user_id)
            if member.status not in ("administrator", "creator"):
                await safe_send_message(ctx, chat_id, "🚫 Admin only.")
                return
        except Exception:
            return

        if not await is_premium_user(user_id):
            await safe_send_message(ctx, chat_id, "🔒 Premium required for scheduling.")
            return

        if len(ctx.args) != 2:
            await safe_send_message(
                ctx, chat_id,
                "📅 <b>Usage</b>\n"
                "• <code>/schedule QUIZ_ID 14:30</code> — regular quiz at that IST time\n"
                "• <code>/schedule QUIZ_ID 15m</code> — persistent poll quiz in 15 minutes\n"
                "Relative poll delays: 1m–30d (examples: 45m, 2h, 1d).",
                parse_mode=ParseMode.HTML,
            )
            return

        qid, schedule_spec = ctx.args[0], ctx.args[1]
        try:
            now = datetime.now(IST)
            sched_time, run_mode = parse_schedule_spec(schedule_spec, now)
        except ValueError as exc:
            await safe_send_message(ctx, chat_id, f"❌ {exc}")
            return

        quiz_repo = QuizRepository(get_db())
        quiz = await quiz_repo.get(qid)
        if not quiz or not quiz.get("questions"):
            await safe_send_message(ctx, chat_id, f"❌ Quiz {qid} not found or has no questions.")
            return

        chat_type = "supergroup" if update.message.chat.type == ChatType.SUPERGROUP else "group"
        allowed, _batch = await resolve_quiz_access(qid, quiz, chat_id, chat_type, user_id, ctx=ctx)
        if not allowed:
            await safe_send_message(ctx, chat_id, "🔒 This chat does not have access to that quiz.")
            return

        poll_settings = None
        if run_mode == "poll":
            settings = await PollQuizSettingsRepository(get_db()).get(user_id)
            poll_settings = {
                "anonymous": bool(settings.get("anonymous", False)),
                "shuffle_questions": bool(settings.get("shuffle_questions", False)),
                "shuffle_options": bool(settings.get("shuffle_options", False)),
                "scoreboard": bool(settings.get("scoreboard", True)),
            }
        thread_id = getattr(update.effective_message, "message_thread_id", None)
        schedule_id = await schedule_mgr.add(
            chat_id, qid, sched_time, user_id, ctx, thread_id=thread_id,
            run_mode=run_mode, poll_settings=poll_settings,
        )

        diff = sched_time - now
        days, remainder = divmod(int(diff.total_seconds()), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        eta = " ".join(part for value, unit in ((days, "d"), (hours, "h"), (minutes, "m")) if value and (part := f"{value}{unit}")) or "less than a minute"
        mode_label = "Persistent poll quiz" if run_mode == "poll" else "Regular quiz"
        await safe_send_message(
            ctx, chat_id,
            f"✅ <b>{mode_label} scheduled!</b>\n\n"
            f"📘 {quiz.get('quiz_name', 'Quiz')} (<code>{qid}</code>)\n"
            f"🆔 Schedule: <code>{schedule_id}</code>\n"
            f"🕒 {sched_time.strftime('%I:%M %p, %d %b')} IST · in {eta}"
            + ("\n♾️ Polls have no expiry timer. /pollstop stops publishing; /pollclose or /pollfinish closes them." if run_mode == "poll" else ""),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("schedule_command error: %s", e, exc_info=True)


async def viewschedule_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/viewschedule` / `/myschedules` -- list this group's pending schedules."""
    chat_id = update.message.chat_id
    try:
        if update.message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            await safe_send_message(ctx, chat_id, "❌ Groups and supergroups only.")
            return

        schedules = await schedule_mgr.get_for_chat(chat_id)
        if not schedules:
            await safe_send_message(ctx, chat_id, "📅 No pending quiz or poll schedules.")
            return

        schedules.sort(key=lambda item: item["scheduled_time"])
        now = datetime.now(IST)
        text = "📅 <b>Pending Quiz & Poll Schedules</b>\n\n"
        for index, item in enumerate(schedules, 1):
            diff = item["scheduled_time"] - now
            if diff.total_seconds() > 0:
                days, remainder = divmod(int(diff.total_seconds()), 86400)
                hours, remainder = divmod(remainder, 3600)
                minutes, _ = divmod(remainder, 60)
                until = " ".join(
                    part for value, unit in ((days, "d"), (hours, "h"), (minutes, "m"))
                    if value and (part := f"{value}{unit}")
                ) or "less than a minute"
            else:
                until = "starting soon"

            mode = "Persistent polls" if item.get("run_mode") == "poll" else "Regular quiz"
            text += (
                f"{index}. <b>{mode}</b> · <code>{item['quiz_id']}</code>\n"
                f"   🕒 {item['scheduled_time'].strftime('%I:%M %p, %d %b')} IST (in {until})\n"
                f"   🆔 <code>{item['job_id']}</code>\n\n"
            )
        text += "Cancel with <code>/cancelschedule SCHEDULE_ID</code>."
        await safe_send_message(ctx, chat_id, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error("viewschedule_command error: %s", e, exc_info=True)


async def cancelschedule_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/cancelschedule QUIZ_ID` -- cancels a pending scheduled quiz."""
    chat_id = update.message.chat_id
    try:
        user_id = update.message.from_user.id
        if update.message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            await safe_send_message(ctx, chat_id, "❌ Groups and supergroups only.")
            return

        try:
            member = await ctx.bot.get_chat_member(chat_id, user_id)
            if member.status not in ("administrator", "creator"):
                await safe_send_message(ctx, chat_id, "\U0001F6AB Admin only.")
                return
        except Exception:
            return

        if not ctx.args:
            await safe_send_message(ctx, chat_id, "Usage: <code>/cancelschedule SCHEDULE_ID</code> (or QUIZ_ID)", parse_mode=ParseMode.HTML)
            return

        target = ctx.args[0]
        schedules = await schedule_mgr.get_for_chat(chat_id)
        for item in schedules:
            if target in (item["job_id"], item["quiz_id"]) and await schedule_mgr.remove(item["job_id"]):
                await safe_send_message(ctx, chat_id, f"✅ Schedule <code>{item['job_id']}</code> cancelled.", parse_mode=ParseMode.HTML)
                return
        await safe_send_message(ctx, chat_id, f"❌ No pending schedule matches {target}.")
    except Exception as e:
        logger.error("cancelschedule_command error: %s", e)


def register(application: Application) -> None:
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler(["viewschedule", "myschedules"], viewschedule_command))
    application.add_handler(CommandHandler("cancelschedule", cancelschedule_command))
