import pytest

pytest.importorskip("telegram")

from quizbot.runner_bot.handlers.poll_quiz import pollquiz_score_rows


def test_pollquiz_score_rows_rank_real_answers_and_track_completion():
    session = {
        "started_at": 100.0,
        "total_questions": 2,
        "quiz": {"correct_mark": 1, "negative_marking": 0.25},
        "polls": {
            "p1": {"correct_option": [1], "question_index": 0},
            "p2": {"correct_option": [0], "question_index": 1},
        },
        "participants": {
            10: {"name": "Aspirant A", "answers": {
                "p1": {"option": [1], "time": 110},
                "p2": {"option": [0], "time": 120},
            }},
            20: {"name": "Aspirant B", "answers": {
                "p1": {"option": [0], "time": 115},
            }},
        },
    }
    rows = pollquiz_score_rows(session)
    assert [row["user_id"] for row in rows] == [10, 20]
    assert rows[0]["correct"] == 2 and rows[0]["wrong"] == 0
    assert rows[0]["answered"] == 2
    assert rows[1]["correct"] == 0 and rows[1]["wrong"] == 1
    assert rows[1]["answered"] == 1
