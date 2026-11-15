"""Timer helpers are pure and can be tested without Telegram credentials."""

from pathlib import Path

import pytest

from quizbot.shared.timers import (
    MAX_QUIZ_TIMER_SECONDS,
    MIN_QUIZ_TIMER_SECONDS,
    TIMER_PRESETS,
    TIMER_PRESET_ROWS,
    clamp_quiz_timer,
    format_timer,
    validate_quiz_timer,
)


def test_requested_timer_presets_are_available():
    assert {60, 90, 120, 150, 180, 240} <= set(TIMER_PRESETS)
    assert {300, 360, 480, 600} <= set(TIMER_PRESETS)


def test_preset_matrix_has_no_duplicates_and_stays_in_supported_range():
    flattened = [seconds for row in TIMER_PRESET_ROWS for seconds, _label in row]
    assert len(flattened) == len(set(flattened))
    assert all(MIN_QUIZ_TIMER_SECONDS <= item <= MAX_QUIZ_TIMER_SECONDS for item in flattened)


@pytest.mark.parametrize("value, expected", [(10, 10), ("60", 60), ("90", 90), ("3600", 3600), ("+120", 120)])
def test_validate_quiz_timer_accepts_supported_whole_seconds(value, expected):
    assert validate_quiz_timer(value) == expected


@pytest.mark.parametrize("value", [None, "", "abc", "90.5", 9, 4, 3601, 100000, True])
def test_validate_quiz_timer_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        validate_quiz_timer(value)


def test_clamp_quiz_timer_is_safe_for_legacy_data():
    assert clamp_quiz_timer(None) == 30
    assert clamp_quiz_timer("bad") == 30
    assert clamp_quiz_timer(1) == 10
    assert clamp_quiz_timer(99999) == 3600


def test_format_timer_renders_minutes_and_seconds():
    assert format_timer(45) == "45s"
    assert format_timer(60) == "1m"
    assert format_timer(90) == "1m 30s"
    assert format_timer(150) == "2m 30s"
    assert format_timer(240) == "4m"
    assert format_timer(600) == "10m"


def test_poll_senders_use_shared_bounds_and_no_old_four_minute_cap():
    root = Path(__file__).resolve().parents[1]
    source = (root / "quizbot/runner_bot/handlers/quiz_play.py").read_text(encoding="utf-8")
    assert source.count("open_period=timer") == 3
    assert source.count("clamp_quiz_timer(") >= 4
    assert "min(int(timer), 240)" not in source
    assert "timer_override" in source
