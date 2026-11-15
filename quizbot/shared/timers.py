"""Shared timer presets and bounds for all quiz launch paths.

Telegram poll timers are measured in seconds. This project intentionally
keeps the interactive timer range conservative (10 seconds to 1 hour), even
though the current Bot API permits longer poll-open periods.
"""

from __future__ import annotations

from typing import Final

MIN_QUIZ_TIMER_SECONDS: Final[int] = 10
MAX_QUIZ_TIMER_SECONDS: Final[int] = 3600

# Labels are kept concise for Telegram inline keyboards. Every preset is
# available in the regular, sectional, AI-generated and PDF-generated flows.
TIMER_PRESET_ROWS: Final[tuple[tuple[tuple[int, str], ...], ...]] = (
    ((10, "10s"), (15, "15s"), (20, "20s"), (30, "30s"), (45, "45s")),
    ((60, "60s (1m)"), (90, "90s (1m 30s)"), (120, "120s (2m)"), (150, "150s (2m 30s)")),
    ((180, "180s (3m)"), (240, "240s (4m)"), (300, "300s (5m)"), (360, "360s (6m)")),
    ((480, "480s (8m)"), (600, "600s (10m)")),
)

TIMER_PRESETS: Final[tuple[int, ...]] = tuple(
    seconds for row in TIMER_PRESET_ROWS for seconds, _label in row
)


def validate_quiz_timer(value: int | str) -> int:
    """Parse a quiz timer and enforce the supported 10s–3600s range."""
    if isinstance(value, bool):
        raise ValueError("Timer must be a whole number of seconds.")
    try:
        seconds = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Timer must be a whole number of seconds.") from exc
    if str(value).strip() not in (str(seconds), f"+{seconds}"):
        raise ValueError("Timer must be a whole number of seconds.")
    if not MIN_QUIZ_TIMER_SECONDS <= seconds <= MAX_QUIZ_TIMER_SECONDS:
        raise ValueError(
            f"Timer must be between {MIN_QUIZ_TIMER_SECONDS} and "
            f"{MAX_QUIZ_TIMER_SECONDS} seconds (1 hour)."
        )
    return seconds


def clamp_quiz_timer(value: int | str) -> int:
    """Safely clamp legacy or externally supplied timer data to supported bounds."""
    try:
        seconds = int(value)
    except (TypeError, ValueError, OverflowError):
        seconds = 30
    return max(MIN_QUIZ_TIMER_SECONDS, min(seconds, MAX_QUIZ_TIMER_SECONDS))


def format_timer(seconds: int | str) -> str:
    """Return a readable duration such as ``90s (1m 30s)``."""
    total = clamp_quiz_timer(seconds)
    if total < 60:
        return f"{total}s"
    minutes, remainder = divmod(total, 60)
    return f"{minutes}m" if remainder == 0 else f"{minutes}m {remainder}s"
