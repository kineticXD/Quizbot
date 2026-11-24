#!/usr/bin/env python3
"""Unified launcher for Quizbot. All quiz-player and migrated creator features
run through one python-telegram-bot Bot API connection. No MTProto login is
required for the supported creator workflows.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from typing import Any

from quizbot.database import close_db, init_db
from quizbot.shared import config
from quizbot.shared.utils.http import close_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("launcher")

for noisy in ("httpx", "httpcore", "apscheduler", "pymongo", "werkzeug"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


async def _run_runner_bot() -> None:
    from quizbot.runner_bot.bot import run_runner_bot

    logger.info("Starting unified quiz and creator features on the Bot API account")
    await run_runner_bot()


async def _run_mini_app() -> None:
    from quizbot.mini_app.server import run_mini_app_server

    logger.info("Starting Telegram Mini App (FastAPI)")
    await run_mini_app_server()


async def main(only: str | None) -> None:
    component = only or "runner"
    problems = config.validate(bot=component)
    if problems:
        for problem in problems:
            logger.error("Configuration problem: %s", problem)
        logger.error("Fix .env using .env.example, then restart.")
        raise SystemExit(2)

    logger.info("Connecting to MongoDB (db=%s)...", config.MONGODB_DB_NAME)
    await init_db(config.MONGODB_URI, config.MONGODB_DB_NAME)
    logger.info("Database ready.")

    tasks: list[asyncio.Task[Any]] = []
    if only in (None, "runner"):
        tasks.append(asyncio.create_task(_run_runner_bot(), name="bot_api_adapter"))
    if only == "miniapp":
        tasks.append(asyncio.create_task(_run_mini_app(), name="mini_app"))
    elif only is None and config.MINI_APP_DOMAIN:
        tasks.append(asyncio.create_task(_run_mini_app(), name="mini_app"))

    # Do not start the Flask health server on the same host PORT as FastAPI.
    # In a normal deployment the Mini App owns PORT when configured; otherwise
    # the small health endpoint provides the HTTP service expected by PaaS.
    if only != "miniapp" and not config.MINI_APP_DOMAIN:
        from keep_alive import keep_alive

        keep_alive()

    stop_event = asyncio.Event()

    def request_shutdown(*_args: object) -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except (NotImplementedError, RuntimeError):
            # Windows event loops may not implement add_signal_handler.
            pass

    stop_task = asyncio.create_task(stop_event.wait(), name="shutdown_signal")
    try:
        done, _pending = await asyncio.wait(
            [*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            if task is stop_task or task.cancelled():
                continue
            error = task.exception()
            if error:
                logger.error("Component %s stopped with an error", task.get_name(), exc_info=error)
            else:
                logger.warning("Component %s exited unexpectedly", task.get_name())
    finally:
        logger.info("Shutting down Quizbot...")
        for task in tasks:
            if not task.done():
                task.cancel()
        if not stop_task.done():
            stop_task.cancel()
        await asyncio.gather(*tasks, stop_task, return_exceptions=True)
        await close_session()
        await close_db()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the unified Telegram Quizbot.")
    parser.add_argument(
        "--only",
        choices=("runner", "miniapp"),
        default=None,
        help="Run one component for debugging (normal deployments run the unified Bot API bot).",
    )
    arguments = parser.parse_args()
    try:
        asyncio.run(main(arguments.only))
    except KeyboardInterrupt:
        pass
