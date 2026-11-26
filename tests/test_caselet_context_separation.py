from quizbot.creator_bot.parsing import parse_question_block, split_question_blocks


def test_caselet_is_stored_once_as_context_and_each_question_is_separate():
    passage = (
        "A CLAT coaching batch has 800 students, each of whom takes at least one "
        "of two mock sections — Legal Reasoning (LR) and Current Affairs (CA). "
        "Exactly 20% of the batch takes both sections, and the ratio of students "
        "taking only LR to those taking only CA is 5 : 3."
    )
    source = passage + "\n\n" + (
        "How many students take only CA?\nA) 200\nB) ✅ 240\nC) 300\nD) 320\n\n"
        "What percentage of the batch takes LR in any combination?\nA) 50%\nB) 62.5%\nC) ✅ 70%\nD) 75%\n\n"
        "The ratio of students taking exactly one section to those taking both is\n"
        "A) ✅ 4 : 1\nB) 3 : 1\nC) 5 : 2\nD) 8 : 3"
    )
    parsed = [parse_question_block(block) for block in split_question_blocks(source)]
    assert len(parsed) == 3
    assert all(item is not None for item in parsed)
    assert all(item["context"] == passage for item in parsed)
    assert [item["question"] for item in parsed] == [
        "How many students take only CA?",
        "What percentage of the batch takes LR in any combination?",
        "The ratio of students taking exactly one section to those taking both is",
    ]
    assert all(passage not in item["question"] for item in parsed)


def test_standalone_question_does_not_gain_a_caselet():
    question = "Which body has the power to interpret the Constitution?\nA) Parliament\nB) ✅ Supreme Court\nC) Election Commission\nD) Finance Commission"
    parsed = parse_question_block(question)
    assert parsed is not None
    assert parsed["context"] is None
    assert parsed["question"].startswith("Which body")
