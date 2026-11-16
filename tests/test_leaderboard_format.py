from quizbot.shared.leaderboard_format import (
    accuracy_percent,
    format_duration,
    format_leaderboard_markdown,
    format_leaderboard_plain,
)


def test_accuracy_is_based_on_attempted_questions_only():
    assert round(accuracy_percent(39, 12), 1) == 76.5
    assert accuracy_percent(0, 0) is None


def test_rich_leaderboard_contains_reference_columns_and_accuracy():
    text = format_leaderboard_markdown(
        "GK Quiz",
        [{
            "name": "NotSoErudite", "correct": 39, "wrong": 12,
            "score": 39, "total_time": 323,
        }],
        total_questions=60,
        status="Final Results",
    )
    assert "| # | Name | ✅ | ❌ | Score | Time | Accuracy |" in text
    assert "| 🥇 | NotSoErudite | 39 | 12 | 39.00 | 5m 23s | 76.5% |" in text
    assert "correct answers ÷ attempted answers" in text


def test_markdown_table_escapes_pipe_and_newline_in_names():
    text = format_leaderboard_markdown(
        "Quiz",
        [{"name": "First | Second\nThird", "correct": 1, "wrong": 1, "score": 0.5}],
    )
    assert "First \\| Second Third" in text
    assert "| 🥇 | First \\| Second Third |" in text


def test_zero_attempts_show_no_accuracy_not_zero_percent():
    text = format_leaderboard_markdown(
        "Quiz", [{"name": "No answers", "correct": 0, "wrong": 0, "score": 0}],
    )
    assert "| 🥇 | No answers | 0 | 0 | 0.00 | 0m 00s | — |" in text


def test_plain_fallback_keeps_accuracy_and_timing_readable():
    text = format_leaderboard_plain(
        "GK Quiz",
        [{"name": "Player", "correct": 3, "wrong": 1, "score": 2.5, "total_time": 3665}],
        total_questions=5,
    )
    assert "Accuracy 75.0%" in text
    assert "1h 01m 05s" in text


def test_duration_formats_sub_hour_time_compactly():
    assert format_duration(323) == "5m 23s"
