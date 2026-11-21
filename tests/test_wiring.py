import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _registered_commands(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    commands = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if name != "CommandHandler" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            commands.add(first.value)
    return commands


def test_core_quiz_player_commands_are_registered():
    commands = _registered_commands(ROOT / "quizbot/runner_bot/handlers/quiz_play.py")

    assert {
        "start", "skip", "pause", "resume", "stop", "slow", "fast", "normal", "timer", "leaderboard"
    } <= commands


def test_runner_registry_includes_quiz_player_module():
    source = (ROOT / "quizbot/runner_bot/handlers/__init__.py").read_text(encoding="utf-8")

    assert "quiz_play" in source
    assert "quiz_play.register(application)" in source or "module.register(application)" in source


def test_creator_help_mentions_unified_player_entry_points():
    source = (ROOT / "quizbot/creator_bot/handlers/admin.py").read_text(encoding="utf-8")

    for command in ("/start <id>", "/skip", "/pause", "/resume", "/stop", "/leaderboard"):
        assert command in source


def test_skip_is_reserved_during_creator_input_sessions():
    creation = (ROOT / "quizbot/creator_bot/handlers/quiz_creation.py").read_text(encoding="utf-8")
    editing = (ROOT / "quizbot/creator_bot/handlers/quiz_editing.py").read_text(encoding="utf-8")
    assert '"skip"' in creation
    assert '"skip"' in editing


def test_skip_wakes_standard_and_pollquiz_question_waits():
    player = (ROOT / "quizbot/runner_bot/handlers/quiz_play.py").read_text(encoding="utf-8")
    poll_quiz = (ROOT / "quizbot/runner_bot/handlers/poll_quiz.py").read_text(encoding="utf-8")
    state = (ROOT / "quizbot/runner_bot/state.py").read_text(encoding="utf-8")

    assert "await _wait_for_skip_or_timeout(skip_event, timer + 1)" in player
    assert "await _wait_for_skip_or_timeout(skip_event, timer + 2)" in player
    assert 'await ctx.bot.stop_poll(chat_id=chat_id, message_id=message_id)' in player
    assert "event.set()" in player
    assert "pollquiz_controls" in state
    assert "skip_active_pollquiz_question" in poll_quiz
    assert "await asyncio.wait_for(skip_event.wait(), timeout=delay)" in poll_quiz
    assert "if update.channel_post is not None" in player
    assert "channel_poll_tasks.get(chat_id) is current_task" in poll_quiz


def test_timer_command_is_documented_for_users():
    creator_help = (ROOT / "quizbot/creator_bot/handlers/admin.py").read_text(encoding="utf-8")
    assert "/timer <seconds>" in creator_help
    botfather = (ROOT / "BOTFATHER_COMMANDS.txt").read_text(encoding="utf-8")
    assert "timer - Set exact per-question timer" in botfather


def test_all_bot_timer_flows_use_shared_presets():
    paths = [
        ROOT / "quizbot/runner_bot/handlers/setup_wizard.py",
        ROOT / "quizbot/runner_bot/handlers/ai_quiz.py",
        ROOT / "quizbot/runner_bot/handlers/pdf_quiz.py",
    ]
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "TIMER_PRESET_ROWS" in source


def test_live_timer_command_is_registered():
    commands = _registered_commands(ROOT / "quizbot/runner_bot/handlers/quiz_play.py")
    assert "timer" in commands


def test_timer_control_is_not_consumed_by_creator_wizards():
    creation = (ROOT / "quizbot/creator_bot/handlers/quiz_creation.py").read_text(encoding="utf-8")
    editing = (ROOT / "quizbot/creator_bot/handlers/quiz_editing.py").read_text(encoding="utf-8")
    assert '"timer"' in creation
    assert '"timer"' in editing
    assert "~filters.command(runner_commands)" in editing


def test_leaderboard_saver_repository_methods_exist():
    source = (ROOT / "quizbot/database/repositories.py").read_text(encoding="utf-8")
    assert "async def record_attempt(" in source
    assert "async def upsert_entry(" in source


def test_quiz_score_saver_uses_real_repository_methods():
    source = (ROOT / "quizbot/runner_bot/handlers/quiz_play.py").read_text(encoding="utf-8")
    assert "await attempt_repo.record_attempt(" in source
    assert "await lb_repo.upsert_entry(" in source
    assert "await mistake_repo.record(uid, mistakes)" in source
    assert "await qstats_repo.bulk_update_wrong_stats" in source


def test_long_passages_use_chunked_plain_text_delivery():
    source = (ROOT / "quizbot/runner_bot/handlers/quiz_play.py").read_text(encoding="utf-8")
    utils = (ROOT / "quizbot/runner_bot/telegram_utils.py").read_text(encoding="utf-8")
    assert "safe_send_long_message(ctx, chat_id, overflow" in source
    assert "from quizbot.shared.text_limits import split_telegram_text" in utils


def test_topic_thread_id_alias_is_normalized_for_ptb():
    source = (ROOT / "quizbot/runner_bot/telegram_utils.py").read_text(encoding="utf-8")
    assert 'explicit_thread_id = kwargs.pop("thread_id", None)' in source
    assert 'kwargs["message_thread_id"] = explicit_thread_id' in source


def test_topic_helper_accepts_update_objects_and_message_ids():
    source = (ROOT / "quizbot/runner_bot/telegram_utils.py").read_text(encoding="utf-8")
    assert "def _get_topic_thread_id(value: Any)" in source
    assert "effective_message" in source
    assert "message_thread_id" in source


def test_pollquiz_has_non_anonymous_answer_tracking_and_finalization():
    state = (ROOT / "quizbot/runner_bot/state.py").read_text(encoding="utf-8")
    poll_quiz = (ROOT / "quizbot/runner_bot/handlers/poll_quiz.py").read_text(encoding="utf-8")
    player = (ROOT / "quizbot/runner_bot/handlers/quiz_play.py").read_text(encoding="utf-8")
    assert "pollquiz_sessions" in state
    assert "handle_pollquiz_poll_answer" in player
    assert "pollquiz_score_rows" in player
    assert "finalize_pollquiz" in poll_quiz
    assert "is_anonymous=is_anonymous" in poll_quiz
    assert "auto_close_seconds" in poll_quiz
    assert "open_period" in poll_quiz
    assert "finalize_pollquiz(ctx, chat_id)" in poll_quiz


def test_pollquiz_scores_use_real_repository_save_path():
    source = (ROOT / "quizbot/runner_bot/handlers/poll_quiz.py").read_text(encoding="utf-8")
    assert "_record_attempt_and_report(" in source
    assert "stop_poll" in source


def test_pollquiz_auto_closes_and_publishes_final_leaderboard():
    source = (ROOT / "quizbot/runner_bot/handlers/poll_quiz.py").read_text(encoding="utf-8")
    assert "POLLQUIZ_AUTO_CLOSE_SECONDS" in source
    assert 'poll_kwargs["open_period"]' in source
    assert "await asyncio.sleep(auto_close_seconds + 7)" in source
    assert "await finalize_pollquiz(ctx, chat_id)" in source
    assert "Usage: /pollquiz QUIZ_ID [minutes]" in source


def test_standard_quiz_stop_publishes_partial_leaderboard():
    source = (ROOT / "quizbot/runner_bot/handlers/quiz_play.py").read_text(encoding="utf-8")
    stop_start = source.index("async def stop_command")
    stop_end = source.index("async def _change_speed", stop_start)
    stop_source = source[stop_start:stop_end]
    assert "end_private_quiz(chat_id, ctx, ended_early=True)" in stop_source
    assert "_end_group_quiz(chat_id, ctx, update, ended_early=True)" in stop_source
    assert "await session_mgr.delete(chat_id)" not in stop_source
    assert "Partial Leaderboard" in source


def test_pollquiz_duration_is_configurable():
    config = (ROOT / "quizbot/shared/config.py").read_text(encoding="utf-8")
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert 'POLLQUIZ_AUTO_CLOSE_SECONDS: int = _env_int("POLLQUIZ_AUTO_CLOSE_SECONDS", 3600)' in config
    assert "POLLQUIZ_AUTO_CLOSE_SECONDS=3600" in env
