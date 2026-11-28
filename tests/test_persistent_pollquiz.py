from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
def test_poll_sender_has_no_open_period():
    s=(ROOT/"quizbot/runner_bot/handlers/poll_quiz.py").read_text()
    assert 'poll_kwargs["open_period"]' not in s
def test_pollquiz_has_no_duration_argument():
    s=(ROOT/"quizbot/runner_bot/handlers/poll_quiz.py").read_text()
    assert 'len(ctx.args) > 2' in s
def test_stop_finalizes_persistent_session():
    s=(ROOT/"quizbot/runner_bot/handlers/poll_quiz.py").read_text()
    assert 'await finalize_pollquiz(ctx, chat_id)' in s
