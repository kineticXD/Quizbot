from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_shikimori_welcome_and_dashboard_buttons_are_registered():
    admin = (ROOT / "quizbot/runner_bot/handlers/admin.py").read_text(encoding="utf-8")
    player = (ROOT / "quizbot/runner_bot/handlers/quiz_play.py").read_text(encoding="utf-8")
    assert "Welcome to Shikimori" in player
    assert "Shikimori" in admin
    for callback in ("shk:create", "shk:library", "shk:polls", "shk:pollsettings", "shk:help"):
        assert callback in admin
    assert "dashboard_callback" in admin


def test_pollstop_does_not_finalize_or_publish_leaderboard():
    source = (ROOT / "quizbot/runner_bot/handlers/poll_quiz.py").read_text(encoding="utf-8")
    start = source.index("async def pollstop_command")
    end = source.index("async def pollclose_command", start)
    stop_handler = source[start:end]
    assert "_stop_poll_publisher" in stop_handler
    assert "finalize_pollquiz" not in stop_handler
    assert "_send_pollquiz_scoreboard" not in stop_handler


def test_help_describes_stop_close_and_finish_separately():
    admin = (ROOT / "quizbot/runner_bot/handlers/admin.py").read_text(encoding="utf-8")
    assert "/pollstop — stop publishing" in admin
    assert "/pollclose — close sent polls without showing a leaderboard" in admin
    assert "/pollfinish — close polls and publish final results" in admin


def test_scheduled_poll_copy_uses_new_close_controls():
    source = (ROOT / "quizbot/runner_bot/handlers/scheduling.py").read_text(encoding="utf-8")
    assert "Polls have no expiry timer" in source
    assert "/pollclose" in source and "/pollfinish" in source
    assert "pollquiz_sessions" in source
