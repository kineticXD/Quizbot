from datetime import datetime, timedelta, timezone
import pytest

from quizbot.runner_bot.schedule_utils import parse_schedule_spec

IST = timezone(timedelta(hours=5, minutes=30))


def test_relative_schedule_spec_is_a_timed_poll_delay():
    now = datetime(2026, 11, 24, 10, 0, tzinfo=IST)
    scheduled, mode = parse_schedule_spec("2h", now)
    assert mode == "poll"
    assert scheduled == now + timedelta(hours=2)


def test_clock_schedule_spec_preserves_regular_quiz_mode_and_rolls_to_tomorrow():
    now = datetime(2026, 11, 24, 18, 0, tzinfo=IST)
    scheduled, mode = parse_schedule_spec("09:30", now)
    assert mode == "quiz"
    assert scheduled.day == 25
    assert (scheduled.hour, scheduled.minute) == (9, 30)


@pytest.mark.parametrize("value", ["0m", "31d", "5x", "25:00", "10:70"])
def test_invalid_schedule_spec_is_rejected(value):
    now = datetime(2026, 11, 24, 10, 0, tzinfo=IST)
    with pytest.raises(ValueError):
        parse_schedule_spec(value, now)
