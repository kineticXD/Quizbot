"""Regression tests for Telegram quiz polls with 2–12 answer options."""

import importlib
import json
import sys
import types
from pathlib import Path

import pytest

from quizbot.creator_bot.parsing import parse_question_block, split_question_blocks


def _load_file_import_module():
    """Load the pure importer without importing the Pyrogram handler registry."""
    package_name = "quizbot.creator_bot.handlers"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [str(Path(__file__).resolve().parents[1] / "quizbot" / "creator_bot" / "handlers")]
        sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.file_import")


def _load_ai_parser(monkeypatch):
    """Load the pure AI response parser without requiring a live Mongo driver."""
    try:
        import pymongo  # noqa: F401
    except ImportError:
        database_stub = types.ModuleType("quizbot.database")
        database_stub.AIKeyRepository = type("AIKeyRepository", (), {})
        database_stub.UserRepository = type("UserRepository", (), {})
        database_stub.get_db = lambda: None
        monkeypatch.setitem(sys.modules, "quizbot.database", database_stub)
    module = importlib.import_module("quizbot.runner_bot.ai_providers")
    return module.parse_ai_questions


def make_mcq(count: int, correct_index: int = 0) -> str:
    letters = "ABCDEFGHIJKL"
    lines = ["Which option is correct?"]
    for index in range(count):
        marker = " ✅" if index == correct_index else ""
        label = letters[index] if index < len(letters) else chr(65 + index)
        lines.append(f"{label}) Choice {label}{marker}")
    return "\n".join(lines)


@pytest.mark.parametrize("count", [2, 3, 4, 5, 6, 8, 10, 11, 12])
def test_text_parser_accepts_two_through_twelve_options(count):
    parsed = parse_question_block(make_mcq(count, correct_index=count - 1))
    assert parsed is not None
    assert len(parsed["options"]) == count
    assert parsed["correct_option_id"] == count - 1
    assert parsed["options"][-1] == f"Choice {'ABCDEFGHIJKL'[count - 1]}"


def test_text_parser_rejects_more_than_twelve_options():
    assert parse_question_block(make_mcq(12) + "\nM) Choice M") is None


def test_splitter_keeps_all_twelve_option_lines_in_same_question_block():
    text = make_mcq(12) + "\n\nWhat comes next?\nA) One\nB) Two ✅"
    blocks = split_question_blocks(text)
    assert len(blocks) == 2
    first = parse_question_block(blocks[0])
    second = parse_question_block(blocks[1])
    assert first is not None and len(first["options"]) == 12
    assert second is not None and len(second["options"]) == 2


def test_json_import_accepts_twelve_and_rejects_thirteen():
    module = _load_file_import_module()
    process_json = module._process_json
    out = []
    valid = {"questions": [{
        "question_text": "Pick the twelfth option.",
        "options": [{"id": f"id{i}", "text": f"Choice {i}"} for i in range(12)],
        "correct_option_id": "id11",
    }]}
    assert process_json(valid, [], out) == 1
    assert len(out[0]["options"]) == 12
    assert out[0]["correct_option_id"] == 11

    invalid = {"questions": [{
        "question_text": "Too many options.",
        "options": [{"id": f"id{i}", "text": f"Choice {i}"} for i in range(13)],
        "correct_option_id": "id12",
    }]}
    assert process_json(invalid, [], []) == 0


def test_ai_json_parser_accepts_twelve_and_preserves_correct_index(monkeypatch):
    parse_ai_questions = _load_ai_parser(monkeypatch)
    payload = [{"q": "Pick the twelfth option", "o": [f"Choice {i}" for i in range(12)], "c": [11], "e": None}]
    parsed = parse_ai_questions(json.dumps(payload))
    assert len(parsed) == 1
    assert len(parsed[0]["options"]) == 12
    assert parsed[0]["correct_option_id"] == 11


def test_ai_text_fallback_recognizes_labels_through_l(monkeypatch):
    parse_ai_questions = _load_ai_parser(monkeypatch)
    parsed = parse_ai_questions(make_mcq(12, correct_index=11))
    assert len(parsed) == 1
    assert len(parsed[0]["options"]) == 12
    assert parsed[0]["correct_option_id"] == 11


def test_ai_json_parser_rejects_thirteen_options(monkeypatch):
    parse_ai_questions = _load_ai_parser(monkeypatch)
    payload = [{"q": "Too many", "o": [f"Choice {i}" for i in range(13)], "c": [12], "e": None}]
    assert parse_ai_questions(json.dumps(payload)) == []


def test_poll_data_accepts_twelve_options_and_rejects_thirteen():
    pytest.importorskip("telegram")
    from quizbot.runner_bot.telegram_utils import prepare_poll_data
    options = [f"Choice {i}" for i in range(12)]
    result = prepare_poll_data("A question?", options, 11, None, None, 0, 1)
    assert len(result[1]) == 12
    with pytest.raises(ValueError, match="2–12 options"):
        prepare_poll_data("A question?", options + ["Choice 12"], 11, None, None, 0, 1)
