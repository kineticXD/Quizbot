"""Regression tests for global owner controls and stop/final leaderboard."""
from __future__ import annotations

import ast
import asyncio
import html
import logging
import typing
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
PLAYER = ROOT / "quizbot/runner_bot/handlers/quiz_play.py"


def _load_function(name: str, namespace: dict):
    tree = ast.parse(PLAYER.read_text(encoding="utf-8"))
    node = next(
        n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(module, str(PLAYER), "exec"), namespace)
    return namespace[name]


def test_configured_owner_and_admin_bypass_group_membership_check():
    config = SimpleNamespace(OWNER_ID=111, ADMIN_IDS=[222])
    ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
    check = _load_function(
        "_require_admin",
        {"config": config, "ContextTypes": ContextTypes, "Optional": typing.Optional},
    )

    class Bot:
        def __init__(self, status="member"):
            self.status = status
            self.calls = []

        async def get_chat_member(self, chat_id, user_id):
            self.calls.append((chat_id, user_id))
            return SimpleNamespace(status=self.status)

    async def scenario():
        owner_bot = Bot("member")
        admin_bot = Bot("member")
        regular_bot = Bot("member")
        group_admin_bot = Bot("administrator")
        assert await check(SimpleNamespace(bot=owner_bot), -100, 111, False) is True
        assert await check(SimpleNamespace(bot=admin_bot), -100, 222, False) is True
        assert await check(SimpleNamespace(bot=regular_bot), -100, 333, False) is False
        assert await check(SimpleNamespace(bot=group_admin_bot), -100, 444, False) is True
        assert owner_bot.calls == [] and admin_bot.calls == []

    asyncio.run(scenario())


def test_stop_finalizer_uses_quiz_data_questions_when_session_has_no_copy():
    class SessionManager:
        def __init__(self, session):
            self.session = session
            self.deleted = False

        def get(self, chat_id):
            return None if self.deleted else self.session

        async def delete(self, chat_id):
            self.deleted = True
            return self.session

    class Bot:
        async def get_chat(self, chat_id):
            return SimpleNamespace(title="Test Group")

    session = {
        # This matches the real setup_wizard/AI launch state: no top-level
        # `questions`, with the question list nested under quiz_data.
        "quiz_data": {
            "qid": "abc123",
            "quiz_name": "Regression Quiz",
            "questions": [{"question": "Q1"}, {"question": "Q2"}],
            "correct_mark": 1,
            "negative_marking": 0,
        },
        "participants": {
            12345: {
                "name": "Aspirant & Friend",
                "answers": {"poll-1": {"option": [1], "time": 105.0}},
            }
        },
        "polls": {
            "poll-1": {"correct_option": [1], "sent_time": 100.0, "question_index": 0}
        },
    }
    sent = []
    persisted_users = []

    async def safe_send_message(ctx, chat_id, text, **kwargs):
        sent.append(text)
        return object()

    async def no_sleep(_seconds):
        return None

    class FakeLogger:
        def error(self, *args, **kwargs):
            raise AssertionError(f"Unexpected finalization error: {args}")

    async def record_attempt_and_report(ctx, chat_id, quiz_data, users, **kwargs):
        persisted_users.extend(users)
        assert len(quiz_data["questions"]) == 2
        return True

    class FakeAsyncio:
        sleep = staticmethod(no_sleep)

    class FakeSessionManager(SessionManager):
        pass

    namespace = {
        "Any": typing.Any,
        "Optional": typing.Optional,
        "ContextTypes": SimpleNamespace(DEFAULT_TYPE=object),
        "asyncio": FakeAsyncio,
        "logger": FakeLogger(),
        "session_mgr": FakeSessionManager(session),
        "_get_topic_thread_id": lambda _value: None,
        "safe_send_message": safe_send_message,
        "ParseMode": SimpleNamespace(HTML="HTML"),
        "escape": html.escape,
        "is_correct": lambda selected, correct: set(selected) == set(correct),
        "_record_attempt_and_report": record_attempt_and_report,
    }
    finalize = _load_function("_end_group_quiz", namespace)

    asyncio.run(finalize(-100, SimpleNamespace(bot=Bot()), object(), ended_early=True))

    assert namespace["session_mgr"].deleted is True
    final_board = next(text for text in sent if "Partial Leaderboard" in text)
    assert "Regression Quiz" in final_board
    assert "2" in final_board
    assert "Aspirant &amp; Friend" in final_board
    assert "1.00 pts" in final_board
    assert "1✅ 0❌" in final_board
    assert len(persisted_users) == 1
    assert persisted_users[0]["user_id"] == 12345
