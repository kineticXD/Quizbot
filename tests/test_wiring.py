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
        "start", "pause", "resume", "stop", "slow", "fast", "normal", "timer", "leaderboard"
    } <= commands


def test_runner_registry_includes_quiz_player_module():
    source = (ROOT / "quizbot/runner_bot/handlers/__init__.py").read_text(encoding="utf-8")

    assert "quiz_play" in source
    assert "quiz_play.register(application)" in source or "module.register(application)" in source


def test_creator_help_mentions_unified_player_entry_points():
    source = (ROOT / "quizbot/creator_bot/handlers/admin.py").read_text(encoding="utf-8")

    for command in ("/start <id>", "/pause", "/resume", "/stop", "/leaderboard"):
        assert command in source


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
