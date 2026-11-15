from quizbot.creator_bot.parsing import parse_question_block, split_question_blocks
from quizbot.shared.text_limits import split_telegram_text


def test_single_passage_with_blank_paragraphs_stays_one_question():
    source = (
        "Read the passage carefully.\n\n"
        "The second paragraph explains the key principle.\n\n"
        "Which conclusion follows?\n\n"
        "A) First option\nB) Correct option ✅\nC) Third option\nD) Fourth option"
    )
    blocks = split_question_blocks(source)
    assert len(blocks) == 1
    parsed = parse_question_block(blocks[0])
    assert parsed is not None
    assert "passage carefully.\n\nThe second paragraph" in parsed["question"]
    assert parsed["correct_option_id"] == 1


def test_two_passage_questions_split_after_option_set_not_blank_paragraph():
    source = (
        "Passage for question one.\n\nMore passage text.\n\nQuestion one?\n"
        "A) One\nB) Two ✅\nC) Three\nD) Four\n\n"
        "Passage for question two.\n\nQuestion two?\n"
        "A) Alpha ✅\nB) Beta\nC) Gamma\nD) Delta"
    )
    blocks = split_question_blocks(source)
    assert len(blocks) == 2
    first = parse_question_block(blocks[0])
    second = parse_question_block(blocks[1])
    assert first and second
    assert "More passage text." in first["question"]
    assert second["question"].startswith("Passage for question two.")


def test_legacy_unlabelled_blocks_still_use_blank_line_separator():
    source = "Question one\n🧩\nAlpha\nBeta ✅\n\nQuestion two\n🧩\nOne ✅\nTwo"
    assert len(split_question_blocks(source)) == 2


def test_telegram_chunker_keeps_all_content_under_utf16_limit():
    source = ("Long passage paragraph with emoji 📚 and punctuation.\n\n" * 300).strip()
    chunks = split_telegram_text(source)
    assert len(chunks) > 1
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 3900 for chunk in chunks)
    assert "".join(chunks) == source


def test_single_emoji_separator_passage_is_not_split_at_blank_paragraphs():
    source = "Passage opening.\n\nPassage continues.\n\n🧩\nFirst option\nCorrect ✅\nThird option"
    blocks = split_question_blocks(source)
    assert blocks == [source]


def test_shared_passage_is_inherited_by_later_questions_in_same_set():
    passage = (
        "Read the passage carefully. The passage describes a constitutional debate "
        "and provides the historical background needed to answer the following questions."
    )
    source = (
        passage + "\n\nWhich statement best reflects the author's argument?\n"
        "A) First option\nB) Correct option ✅\nC) Third option\nD) Fourth option\n\n"
        "According to the passage, what follows from the debate?\n"
        "A) Alpha\nB) Beta ✅\nC) Gamma\nD) Delta"
    )
    blocks = split_question_blocks(source)
    assert len(blocks) == 2
    first = parse_question_block(blocks[0])
    second = parse_question_block(blocks[1])
    assert first and second
    assert passage in second["question"]
    assert "According to the passage" in second["question"]


def test_option_labels_without_space_are_parsed():
    source = "Choose one\nA)Alpha\nB)Beta ✅\nC)Gamma\nD)Delta"
    parsed = parse_question_block(source)
    assert parsed is not None
    assert parsed["options"] == ["Alpha", "Beta", "Gamma", "Delta"]
    assert parsed["correct_option_id"] == 1


def test_shared_passage_switches_when_a_second_passage_starts():
    passage_one = "First passage: " + ("A long constitutional discussion. " * 3)
    passage_two = "Second passage: " + ("A separate economic discussion. " * 3)
    source = (
        passage_one + "\n\nWhat follows from the first passage?\n"
        "A) One\nB) Two ✅\nC) Three\nD) Four\n\n"
        + passage_two + "\n\nWhich conclusion follows from the second passage?\n"
        "A) Alpha\nB) Beta ✅\nC) Gamma\nD) Delta\n\n"
        "According to the second passage, what is the result?\n"
        "A) Red\nB) Blue ✅\nC) Green\nD) Yellow"
    )
    blocks = split_question_blocks(source)
    assert len(blocks) == 3
    parsed_second = parse_question_block(blocks[1])
    parsed_third = parse_question_block(blocks[2])
    assert parsed_second and parsed_third
    assert passage_two.strip() in parsed_second["question"]
    assert passage_two.strip() in parsed_third["question"]
    assert passage_one.strip() not in parsed_third["question"]
