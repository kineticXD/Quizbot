from quizbot.creator_bot.source_quiz_engine import parse_structured_quiz, validate_ai_quiz


def test_source_engine_parses_checkmarked_mcqs_and_shared_passage():
    source = (
        "Directions (1-1): Use the passage.\n"
        "The Treaty of Versailles was signed in 1919.\n\n"
        "1. In which year was the treaty signed?\n"
        "A) 1918\nB) 1919 ✅\nC) 1920\nD) 1921"
    )
    questions, warnings, context = parse_structured_quiz(source)

    assert warnings == []
    assert len(questions) == 1
    assert questions[0]["correct_id"] == 1
    assert questions[0]["options"][1] == "1919"
    assert "Treaty of Versailles" in context
    assert "1919" in questions[0]["context"]


def test_source_ai_validation_rejects_evidence_not_present_in_source():
    source = "The first successful powered flight took place in 1903 at Kitty Hawk."
    invalid = {
        "questions": [{
            "question": "In which year did the first powered flight take place?",
            "options": ["1901", "1902", "1903", "1904"],
            "answer": "1903",
            "evidence": "The first powered flight occurred in 1905.",
            "explanation": "The passage gives the year.",
        }]
    }
    try:
        validate_ai_quiz(invalid, source, limit=5)
    except ValueError as exc:
        assert "evidence" in str(exc).lower() or "passed" in str(exc).lower()
    else:
        raise AssertionError("Evidence not present in the source should be rejected")


def test_source_ai_validation_accepts_exact_source_evidence():
    source = "The first successful powered flight took place in 1903 at Kitty Hawk."
    valid = {
        "title": "Flight history",
        "questions": [{
            "question": "In which year did the first successful powered flight take place?",
            "options": ["1901", "1902", "1903", "1904"],
            "answer": "1903",
            "evidence": "The first successful powered flight took place in 1903",
            "explanation": "The source explicitly states the year.",
        }]
    }
    questions = validate_ai_quiz(valid, source, limit=5)
    assert len(questions) == 1
    assert questions[0]["correct_id"] == 2
