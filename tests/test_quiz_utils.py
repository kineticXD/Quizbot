"""Pure quiz helpers are tested without connecting to MongoDB."""
import importlib
import sys
import types

import pytest


@pytest.fixture(scope="module")
def quiz_utils():
    # quiz_utils imports repository classes only for its async access helpers.
    # Stub that boundary so these deterministic pure helpers need no database.
    database_stub = types.ModuleType("quizbot.database")
    for name in ("AuthChatRepository", "BatchRepository", "QuizRepository", "get_db"):
        setattr(database_stub, name, object)
    previous = sys.modules.get("quizbot.database")
    sys.modules["quizbot.database"] = database_stub
    try:
        module = importlib.import_module("quizbot.runner_bot.quiz_utils")
        yield module
    finally:
        if previous is None:
            sys.modules.pop("quizbot.database", None)
        else:
            sys.modules["quizbot.database"] = previous


def test_multi_answer_check_requires_exact_set(quiz_utils):
    assert quiz_utils.is_correct([2, 0], [0, 2]) is True
    assert quiz_utils.is_correct([0, 1, 2], [0, 2]) is False
    assert quiz_utils.is_correct([0], [0, 2]) is False


def test_single_answer_check_accepts_scalar_or_singleton_list(quiz_utils):
    assert quiz_utils.is_correct(2, 2) is True
    assert quiz_utils.is_correct([2], 2) is True
    assert quiz_utils.is_correct(1, 2) is False


def test_shuffle_options_remaps_correct_index(quiz_utils, monkeypatch):
    monkeypatch.setattr(quiz_utils.random, "shuffle", lambda items: items.reverse())

    options, correct = quiz_utils.shuffle_options(["A", "B", "C"], 0)

    assert options == ["C", "B", "A"]
    assert correct == 2


def test_shuffle_options_multi_remaps_selected_head_and_keeps_tail(quiz_utils, monkeypatch):
    monkeypatch.setattr(quiz_utils.random, "shuffle", lambda items: items.reverse())

    options, correct = quiz_utils.shuffle_options_multi(["A", "B", "C", "D"], [0, 2], count=2)

    assert options == ["B", "A", "C", "D"]
    assert correct == [1, 2]


def test_shuffle_options_multi_can_shuffle_everything(quiz_utils, monkeypatch):
    monkeypatch.setattr(quiz_utils.random, "shuffle", lambda items: items.reverse())

    options, correct = quiz_utils.shuffle_options_multi(["A", "B", "C"], [0, 2])

    assert options == ["C", "B", "A"]
    assert correct == [2, 0]


def test_shuffle_rejects_invalid_correct_index(quiz_utils):
    with pytest.raises(ValueError):
        quiz_utils.shuffle_options(["A", "B"], 2)
    with pytest.raises(ValueError):
        quiz_utils.shuffle_options_multi(["A", "B"], [3])


def test_empty_multi_options_are_handled_safely(quiz_utils):
    assert quiz_utils.shuffle_options_multi([], []) == ([], [])
    with pytest.raises(ValueError):
        quiz_utils.shuffle_options([], 0)


def test_section_lookup_uses_zero_based_question_index(quiz_utils):
    sections = [
        {"name": "A", "question_range": [1, 5]},
        {"name": "B", "question_range": [6, 10]},
    ]

    assert quiz_utils.get_section_for_question(sections, 0)["name"] == "A"
    assert quiz_utils.get_section_for_question(sections, 4)["name"] == "A"
    assert quiz_utils.get_section_for_question(sections, 5)["name"] == "B"
    assert quiz_utils.get_section_for_question(sections, 10) is None


def test_section_marks_fall_back_to_global_values(quiz_utils):
    assert quiz_utils.section_marks(None, 2, 0.5) == (2, 0.5)
    assert quiz_utils.section_marks({"correct_mark": 3}, 2, 0.5) == (3, 0.5)
