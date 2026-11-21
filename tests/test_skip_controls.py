"""Focused tests for the independent /pollquiz skip control."""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "quizbot/runner_bot/handlers/poll_quiz.py"


def _load_skip_helper(namespace: dict):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "skip_active_pollquiz_question"
    )
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace[function.name]


def _namespace(controls, tasks):
    return {
        "asyncio": asyncio,
        "ContextTypes": SimpleNamespace(DEFAULT_TYPE=object),
        "pollquiz_controls": controls,
        "channel_poll_tasks": tasks,
        "logger": logging.getLogger("test_skip_controls"),
    }


def test_skip_active_pollquiz_closes_current_poll_and_signals_next_question():
    async def run():
        chat_id = -1001
        event = asyncio.Event()
        control = {
            "skip_event": event,
            "active_poll_id": "poll-123",
            "active_poll_message_id": 456,
            "current_index": 2,
            "skip_requested": False,
            "posting_complete": False,
        }
        task = asyncio.create_task(asyncio.sleep(30))
        closed = []

        class Bot:
            async def stop_poll(self, **kwargs):
                closed.append(kwargs)

        helper = _load_skip_helper(_namespace({chat_id: control}, {chat_id: task}))
        try:
            ok, message = await helper(SimpleNamespace(bot=Bot()), chat_id)
            assert ok is True
            assert event.is_set()
            assert control["skip_requested"] is True
            assert closed == [{"chat_id": chat_id, "message_id": 456}]
            assert "Question 3 skipped" in message
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_skip_active_pollquiz_rejects_when_question_posting_is_complete():
    async def run():
        chat_id = -1002
        event = asyncio.Event()
        control = {
            "skip_event": event,
            "active_poll_id": None,
            "active_poll_message_id": None,
            "current_index": None,
            "skip_requested": False,
            "posting_complete": True,
        }
        task = asyncio.create_task(asyncio.sleep(30))
        closed = []

        class Bot:
            async def stop_poll(self, **kwargs):
                closed.append(kwargs)

        helper = _load_skip_helper(_namespace({chat_id: control}, {chat_id: task}))
        try:
            ok, message = await helper(SimpleNamespace(bot=Bot()), chat_id)
            assert ok is False
            assert "Use /pollstop" in message
            assert closed == []
            assert not event.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_skip_active_pollquiz_resets_flag_when_telegram_cannot_close_poll():
    async def run():
        chat_id = -1003
        event = asyncio.Event()
        control = {
            "skip_event": event,
            "active_poll_id": "poll-789",
            "active_poll_message_id": 789,
            "current_index": 0,
            "skip_requested": False,
            "posting_complete": False,
        }
        task = asyncio.create_task(asyncio.sleep(30))

        class Bot:
            async def stop_poll(self, **kwargs):
                raise RuntimeError("poll already closed")

        helper = _load_skip_helper(_namespace({chat_id: control}, {chat_id: task}))
        try:
            ok, message = await helper(SimpleNamespace(bot=Bot()), chat_id)
            assert ok is False
            assert "couldn't close" in message
            assert control["skip_requested"] is False
            assert not event.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
