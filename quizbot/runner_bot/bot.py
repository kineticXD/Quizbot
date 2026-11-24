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
    schedule_manager = init_schedule_manager(scheduler, application)
    scheduler.start()
    await schedule_manager.restore()
    tasks.spawn(watchdog_loop(), name="watchdog")
    logger.info("Unified Bot API post_init complete (scheduler + watchdog started).")


async def post_shutdown(application: Application) -> None:
    """Tear down resources started by post_init in the manual PTB lifecycle."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
    logger.info("Unified Bot API post_shutdown complete.")


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
    """Run the unified Bot API application.

    Creator and quiz-player handlers are registered on this single PTB
    application, so no separate MTProto authorization or polling process is
    needed. The manual lifecycle invokes PTB startup/shutdown hooks explicitly.
    """
    application = build_application()
    await application.initialize()
    try:
        await application.start()
        await post_init(application)
        await application.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )
        logger.info("Unified Bot API polling started.")

        # Block until run.py cancels this task on SIGINT/SIGTERM or a sibling
        # adapter exits unexpectedly.
        await asyncio.Future()
    finally:
        logger.info("Unified Bot API adapter shutting down...")
        if application.updater and application.updater.running:
            await application.updater.stop()
        if application.running:
            await application.stop()
        await post_shutdown(application)
        await application.shutdown()
