"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import Application

from quizbot.shared import config

from . import handlers
from .handlers.admin import watchdog_loop
from .handlers.scheduling import init_schedule_manager
from .state import tasks

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def post_init(application: Application) -> None:
    """Runs once after Application.initialize() -- starts the scheduler and
    the background watchdog task."""
    init_schedule_manager(scheduler)
    scheduler.start()
    tasks.spawn(watchdog_loop(), name="watchdog")
    logger.info("Quiz-player adapter post_init complete (scheduler + watchdog started).")


async def post_shutdown(application: Application) -> None:
    """Runs once during Application.shutdown() -- tears down the scheduler."""
    scheduler.shutdown(wait=False)
    logger.info("Quiz-player adapter post_shutdown complete.")


def build_application() -> Application:
    """Construct the PTB Application with every handler registered."""
    application = (
        Application.builder()
        .token(config.RUNNER_BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    handlers.register(application)
    return application


async def run_runner_bot() -> None:
    """Run the quiz-player adapter non-blocking alongside the creator adapter.

    Uses the manual PTB lifecycle (initialize/start/start_polling) instead
    of `run_polling()`, which owns its own event loop and would block the
    Kurigram creator adapter from running alongside it in this process.
    """
    application = build_application()

    await application.initialize()
    await application.start()
    await application.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )
    logger.info("Quiz-player adapter polling started.")

    try:
        # Block here until this task is cancelled by the launcher (run.py),
        # which happens on SIGINT/SIGTERM or if the sibling bot task dies.
        await asyncio.Future()
    finally:
        logger.info("Quiz-player adapter shutting down...")
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
