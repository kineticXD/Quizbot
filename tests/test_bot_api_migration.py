import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_launcher_starts_one_bot_api_adapter_only():
    source = (ROOT / "run.py").read_text(encoding="utf-8")
    assert "_run_creator_bot" not in source
    assert "bot_api_adapter" in source
    assert 'choices=("runner", "miniapp")' in source


def test_creator_modules_are_registered_on_runner_application():
    source = (ROOT / "quizbot/runner_bot/handlers/__init__.py").read_text(encoding="utf-8")
    for module in ("creator", "creator_batches", "creator_reports", "creator_admin"):
        assert module in source


def test_live_requirements_do_not_install_mtproto_client():
    source = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "kurigram" not in source
    assert "pyrogram" not in source
    assert "tgcrypto" not in source


def test_creator_creation_commands_are_bot_api_registered():
    source = (ROOT / "quizbot/runner_bot/handlers/creator.py").read_text(encoding="utf-8")
    for command in ("create", "done", "cancel", "sourcequiz", "myquizzes", "edit", "creatorstatus"):
        assert f'("{command}",' in source or f'("{command}", ' in source


def test_batch_edit_field_callback_extracts_user_id_before_field():
    from quizbot.shared.callback_utils import batch_callback_target_user_id

    assert batch_callback_target_user_id(["bb", "editfield", "BATCH1", "123456", "payment_link"], "editfield") == 123456
    assert batch_callback_target_user_id(["bb", "view", "BATCH1", "123456"], "view") == 123456


def test_creation_keeps_quick_save_sections_and_promo_workflow():
    source = (ROOT / "quizbot/runner_bot/handlers/creator.py").read_text(encoding="utf-8")
    assert '"quiz_defaults"' in source
    assert '"awaiting_section_count"' in source
    assert '"awaiting_question_range"' in source
    assert '"question_range"' in source
    assert 'promo_message=session.get("promo_message")' in source
    assert 'pattern=r"^cqd:"' in source


def test_paid_quiz_batch_attachment_is_handled_on_bot_api():
    creator = (ROOT / "quizbot/runner_bot/handlers/creator.py").read_text(encoding="utf-8")
    batches = (ROOT / "quizbot/runner_bot/handlers/creator_batches.py").read_text(encoding="utf-8")
    assert 'callback_data=f"bb:attachquiz:{qid}:{user.id}"' in creator
    assert 'if action == "attachquiz":' in batches
    assert 'if action == "doattach":' in batches
    assert 'if action == "newforquiz":' in batches
