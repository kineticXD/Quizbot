"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

from pyrogram import Client

from quizbot.shared import config

logger = logging.getLogger(__name__)

# Telegram occasionally returns HTTP/RPC 500 errors while authorizing a bot
# over MTProto. Pyrogram retries a single request internally, but after those
# retries it raises TimeoutError. Retry the whole client startup with a longer
# backoff so a temporary Telegram-side failure does not take down the Bot API
# quiz-player adapter in the unified process.
CREATOR_START_RETRY_BASE_SECONDS = 5
CREATOR_START_RETRY_MAX_SECONDS = 60


def _log_safe_config_diagnostics() -> None:
    """Log credential shape/presence only; never print credential contents.

    This is useful on hosts such as Render Free where an interactive shell is
    unavailable and startup logs are the only practical diagnostic channel.
    """
    api_id = config.API_ID
    api_hash = config.API_HASH or ""
    token = config.CREATOR_BOT_TOKEN or ""

    api_id_valid = isinstance(api_id, int) and not isinstance(api_id, bool) and api_id > 0
    api_hash_hex = bool(re.fullmatch(r"[0-9a-fA-F]{32}", api_hash))
    api_hash_has_whitespace = api_hash != api_hash.strip()
    token_format_ok = (
        token.count(":") == 1
        and token.split(":", 1)[0].isdigit()
        and bool(token.split(":", 1)[1])
        and not any(character.isspace() for character in token)
    )

    logger.info(
        "Creator MTProto config diagnostic (values hidden): "
        "API_ID_present=%s API_ID_positive_integer=%s "
        "API_HASH_present=%s API_HASH_length=%d API_HASH_32_hex=%s "
        "API_HASH_has_outer_whitespace=%s BOT_TOKEN_present=%s "
        "BOT_TOKEN_format_shape_ok=%s",
        api_id is not None,
        api_id_valid,
        bool(api_hash),
        len(api_hash),
        api_hash_hex,
        api_hash_has_whitespace,
        bool(token),
        token_format_ok,
    )

    primary_token = os.getenv("BOT_TOKEN")
    legacy_creator_token = os.getenv("CREATOR_BOT_TOKEN")
    legacy_runner_token = os.getenv("RUNNER_BOT_TOKEN")
    if primary_token and (
        (legacy_creator_token and legacy_creator_token != primary_token)
        or (legacy_runner_token and legacy_runner_token != primary_token)
    ):
        logger.warning(
            "Multiple bot-token environment variables differ. The unified config "
            "prioritizes BOT_TOKEN for both adapters. Token values are hidden."
        )

    problems: list[str] = []
    if not api_id_valid:
        problems.append("API_ID must be a positive integer")
    if not api_hash_hex:
        problems.append("API_HASH should be the 32-character hexadecimal value from my.telegram.org")
    if api_hash_has_whitespace:
        problems.append("API_HASH has leading/trailing whitespace")
    if not token_format_ok:
        problems.append("BOT_TOKEN does not have the expected numeric-id:token format or contains whitespace")

    if problems:
        logger.error("Creator MTProto credential checks: %s", "; ".join(problems))
    else:
        logger.info("Creator MTProto credential format checks passed (credentials remain hidden).")


def _is_transient_start_error(error: BaseException) -> bool:
    """Return True for network failures and Telegram 5xx RPC errors."""
    if isinstance(error, (TimeoutError, OSError, ConnectionError)):
        return True

    # Pyrogram/Kurigram RPC exceptions expose the server status through CODE
    # (some compatible forks expose it as `code`). Avoid importing a concrete
    # RPC exception class so this remains compatible with both distributions.
    code: Any = getattr(error, "CODE", None)
    if code is None:
        code = getattr(error, "code", None)
    try:
        return int(code) >= 500
    except (TypeError, ValueError):
        return False


def _retry_delay(attempt: int) -> int:
    """Exponential startup retry delay, capped to avoid a tight retry loop."""
    exponent = max(0, min(attempt - 1, 4))
    return min(
        CREATOR_START_RETRY_MAX_SECONDS,
        CREATOR_START_RETRY_BASE_SECONDS * (2 ** exponent),
    )


async def _wait_for_retry_or_stop(stop_event: asyncio.Event, delay: int) -> bool:
    """Wait for either shutdown or the retry delay; return True on shutdown."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
        return True
    except TimeoutError:
        return False


def build_client() -> Client:
    """Construct (but do not start) the Creator Bot's Pyrogram client."""
    return Client(
        "creator_bot",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.CREATOR_BOT_TOKEN,
        in_memory=True,
        workers=50,
    )


async def run_creator_bot(stop_event: asyncio.Event | None = None) -> None:
    """Start the Creator Bot and block until shutdown.

    A temporary MTProto authorization outage is retried with exponential
    backoff. This is important in the unified deployment: the quiz-player
    adapter uses the Bot API and should remain available while Telegram's
    separate MTProto authorization endpoint is having problems.

    The client is recreated for each retry because a failed ``start()`` may
    leave its internal session stopped. Handler registration remains local to
    each new Client instance.
    """
    _log_safe_config_diagnostics()
    own_event = stop_event or asyncio.Event()
    attempt = 0

    while not own_event.is_set():
        app = build_client()

        from .handlers import register

        register(app)

        try:
            await app.start()
            me = await app.get_me()
            logger.info("Creator adapter started as @%s", me.username)
            attempt = 0
            await own_event.wait()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not _is_transient_start_error(exc):
                raise

            attempt += 1
            delay = _retry_delay(attempt)
            logger.warning(
                "Creator MTProto startup failed with a transient Telegram/network "
                "error (%s). The Bot API quiz-player can continue running; "
                "retrying creator startup in %s seconds (attempt %s).",
                exc,
                delay,
                attempt,
                exc_info=True,
            )
            if await _wait_for_retry_or_stop(own_event, delay):
                return
        finally:
            # A successful start leaves the Client connected; a failed
            # authorization attempt may already have closed its own session.
            # Check the public state before attempting cleanup.
            if getattr(app, "is_connected", False):
                try:
                    await app.stop()
                    logger.info("Creator adapter client stopped.")
                except Exception:
                    logger.debug("Error while cleaning up creator client", exc_info=True)

    logger.info("Creator adapter shutdown requested before startup completed.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_creator_bot())
