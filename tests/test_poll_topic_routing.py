from pathlib import Path

from quizbot.shared.poll_target import resolve_poll_destination

ROOT = Path(__file__).resolve().parents[1]


def test_current_forum_topic_takes_priority_over_saved_topic():
    settings = {"poll_target_chat_id": -10011, "poll_target_thread_id": 222}
    assert resolve_poll_destination(-10011, "supergroup", 333, settings) == (
        -10011, 333, "current_topic"
    )


def test_saved_topic_is_used_from_group_general_chat():
    settings = {"poll_target_chat_id": -10011, "poll_target_thread_id": 222}
    assert resolve_poll_destination(-10011, "supergroup", None, settings) == (
        -10011, 222, "saved_topic"
    )


def test_saved_topic_is_usable_from_private_chat():
    settings = {"poll_target_chat_id": -10011, "poll_target_thread_id": 222}
    assert resolve_poll_destination(12345, "private", None, settings) == (
        -10011, 222, "saved_topic"
    )


def test_saved_topic_is_not_applied_to_an_unrelated_group():
    settings = {"poll_target_chat_id": -10011, "poll_target_thread_id": 222}
    assert resolve_poll_destination(-10022, "supergroup", None, settings) == (
        -10022, None, "current_chat"
    )


def test_polltarget_is_registered_and_documented():
    source = (ROOT / "quizbot/runner_bot/handlers/poll_quiz.py").read_text(encoding="utf-8")
    help_source = (ROOT / "quizbot/runner_bot/handlers/admin.py").read_text(encoding="utf-8")
    botfather = (ROOT / "BOTFATHER_COMMANDS.txt").read_text(encoding="utf-8")
    assert 'CommandHandler("polltarget", polltarget_command)' in source
    assert "Open the exact topic" in source
    assert "/polltarget" in help_source
    assert "polltarget - Choose the forum topic" in botfather


def test_poll_topic_target_is_persisted_in_user_settings():
    source = (ROOT / "quizbot/database/repositories.py").read_text(encoding="utf-8")
    assert '"poll_target_chat_id": None' in source
    assert '"poll_target_thread_id": None' in source
    assert "async def set_topic_target(" in source
    assert "async def clear_topic_target(" in source


def test_topic_target_is_passed_to_poll_publisher():
    source = (ROOT / "quizbot/runner_bot/handlers/poll_quiz.py").read_text(encoding="utf-8")
    assert "thread_id=thread_id" in source
    assert "message_thread_id" in source
    assert "destination_check = await safe_send_message" in source


def test_poll_settings_repository_is_exported_from_database_package():
    source = (ROOT / "quizbot/database/__init__.py").read_text(encoding="utf-8")
    assert "PollQuizSettingsRepository" in source


def test_failed_poll_publication_reports_send_poll_permission():
    source = (ROOT / "quizbot/runner_bot/handlers/poll_quiz.py").read_text(encoding="utf-8")
    assert "Chat Permissions → Send Polls" in source
    assert "Published {published_count} of {total} polls" in source


def test_bot_does_not_need_forum_admin_rights_in_topic_selection_copy():
    source = (ROOT / "quizbot/runner_bot/handlers/poll_quiz.py").read_text(encoding="utf-8")
    assert "does not need administrator status" in source
    assert "Chat Permissions → Send Polls" in source
