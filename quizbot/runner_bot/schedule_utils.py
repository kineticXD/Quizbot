"""Pure parsing helpers for regular and timed-poll schedule commands."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# India does not observe daylight saving time. A fixed IST offset keeps this
# tiny helper dependency-free and works on older Windows/Python installations.
IST_FIXED = timezone(timedelta(hours=5, minutes=30), name="IST")
_DURATION_RE = re.compile(r"^(\d+)([mhd])$", re.IGNORECASE)
MAX_SCHEDULE_SECONDS = 30 * 24 * 60 * 60


def parse_schedule_spec(value: str, now: datetime | None = None) -> tuple[datetime, str]:
    """Parse `15m`/`2h`/`1d` as a timed-poll delay or HH:MM as a regular quiz."""
    current = now or datetime.now(IST_FIXED)
    if current.tzinfo is None:
        current = current.replace(tzinfo=IST_FIXED)
    else:
        current = current.astimezone(IST_FIXED)

    match = _DURATION_RE.fullmatch(str(value or "").strip())
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        multiplier = {"m": 60, "h": 3600, "d": 86400}[unit]
        delay = amount * multiplier
        if not 60 <= delay <= MAX_SCHEDULE_SECONDS:
            raise ValueError("Poll schedules must be between 1 minute and 30 days.")
        return current + timedelta(seconds=delay), "poll"

    try:
        hour, minute = map(int, str(value).split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Use HH:MM for a regular quiz or 15m / 2h / 1d for a timed poll.")

    scheduled = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if scheduled <= current:
        scheduled += timedelta(days=1)
    return scheduled, "quiz"
