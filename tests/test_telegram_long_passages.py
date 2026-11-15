import pytest

pytest.importorskip("telegram")

from quizbot.runner_bot.telegram_utils import prepare_poll_data


def test_long_poll_passage_becomes_complete_overflow_not_truncated():
    passage = ("A long passage with useful details and reasoning. " * 160).strip()
    poll_q, poll_options, _explanation, overflow, description = prepare_poll_data(
        passage + "\n\nWhat is the central idea?",
        ["A short option", "Another short option"], 0, None, None, 0, 1,
    )
    assert len(poll_q) <= 295
    assert overflow is not None
    assert passage in overflow
    assert description is None


def test_overlong_options_are_preserved_in_overflow():
    long_option = "Option detail " * 20
    result = prepare_poll_data(
        "Choose one:", [long_option, "Short option"], 0, None, None, 0, 1
    )
    assert result[1] == ["Option A", "Option B"]
    assert long_option in (result[3] or result[4] or "")


def test_emoji_heavy_passage_and_options_respect_utf16_limits():
    emoji_passage = "📚" * 180
    long_option = "🧠" * 80
    poll_q, poll_options, _explanation, overflow, description = prepare_poll_data(
        emoji_passage, [long_option, "Short option"], 0, None, None, 0, 1,
    )
    assert len(poll_q.encode("utf-16-le")) // 2 <= 295
    assert poll_options == ["Option A", "Option B"]
    assert emoji_passage in (overflow or description or "")
    assert long_option in (overflow or description or "")
