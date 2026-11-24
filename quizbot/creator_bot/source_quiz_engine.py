"""Source-aware quiz parsing and conservative paragraph extraction.

Adapted from the poll-quiz-bot engine for Quizbot's existing quiz schema.
AI calls are deliberately handled by Quizbot's shared provider layer so the
user's saved Gemini/Groq/OpenRouter keys and fallback provider are reused.
"""
from __future__ import annotations

import json
import re
import difflib
import html as html_lib
from typing import Any


CHECK_MARKS = ("✅", "✔️", "✔", "☑️", "☑", "✓")
MAX_POLL_OPTIONS = 12  # Telegram Bot API 9.1; python-telegram-bot >= 22.3
NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}
NUMBER_WORD_PATTERN = "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
OPTION_RE = re.compile(
    r"^\s*(?:\(([A-Za-z])\)|([A-Za-z])[.)]|([A-Za-z])\s*[:\-])\s*(.*?)\s*$"
)
QUESTION_MARKER_RE = re.compile(r"^\s*(?:(?:Q(?:uestion)?\s*)?\d{1,3}[.)\]:-]|Q(?:uestion)?\s*\d+\s*[:.)-])\s*", re.I)
ANSWER_RE = re.compile(r"^\s*(?:correct\s+answer|correct\s+option|answer|ans|correct)\s*[:\-]\s*(.*?)\s*$", re.I)
EXPLANATION_RE = re.compile(r"^\s*(?:explanation|reason|solution|why|ex)\s*[:\-]\s*(.*?)\s*$", re.I)


def _clean_line(line: str) -> str:
    line = line.replace("\u200b", "").replace("\ufeff", "")
    line = re.sub(r"^\s*[-•]\s+", "", line)
    return line.rstrip()


def _strip_marker(line: str) -> tuple[str, bool]:
    marked = any(mark in line for mark in CHECK_MARKS) or bool(re.search(r"\s*(?:\[correct\]|\(correct\))\s*$", line, flags=re.I))
    for mark in CHECK_MARKS:
        line = line.replace(mark, "")
    line = re.sub(r"\s*(?:\[correct\]|\(correct\))\s*$", "", line, flags=re.I)
    return line.strip(), marked


def _resolve_answer(answer_text: str, options: list[str]) -> int | None:
    answer_text = answer_text.strip()
    answer_text = re.sub(r"^(?:option|choice)\s+", "", answer_text, flags=re.I)
    m = re.match(r"^\(?([A-Za-z])\)?(?:\s|[).:-]|$)", answer_text)
    if m:
        index = ord(m.group(1).upper()) - ord("A")
        if 0 <= index < len(options):
            return index
    m = re.match(r"^(\d{1,2})(?:\s|[).:-]|$)", answer_text)
    if m:
        index = int(m.group(1)) - 1
        if 0 <= index < len(options):
            return index
    normalized = re.sub(r"\s+", " ", answer_text).strip(" .") .casefold()
    for i, option in enumerate(options):
        if normalized and normalized == re.sub(r"\s+", " ", option).strip(" .").casefold():
            return i
    return None


def _normalize_import_text(text: str) -> list[str]:
    """Normalize copied chat/document text without discarding meaningful lines."""
    text = html_lib.unescape(str(text or ""))
    text = text.replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Some sources encode a hard line break as a trailing backslash.
    text = re.sub(r"\\[ \t]*\n", "\n", text)
    lines = []
    for raw in text.split("\n"):
        line = raw.rstrip()
        # A trailing slash at end-of-input is also a copied hard-break marker.
        if line.endswith("\\"):
            line = line[:-1].rstrip()
        # Remove common Markdown emphasis while preserving the words themselves.
        line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
        line = re.sub(r"__(.*?)__", r"\1", line)
        lines.append(_clean_line(line))
    return lines


DIRECTIONS_RE = re.compile(
    r"^\s*directions?\s*(?:\(\s*(?P<first>\d{1,3})\s*[-–—]\s*(?P<last>\d{1,3})\s*\))?\s*:\s*(?P<body>.*)$",
    re.I,
)
QUESTION_START_RE = re.compile(
    r"^(?:what|which|who|whom|whose|when|where|why|how|if|after|before|assuming|calculate|compute|find|identify|select|choose|determine|state|give|in how many|to what extent|under what)\b",
    re.I,
)


def _is_question_stem(line: str, has_context: bool = False) -> bool:
    candidate = line.strip()
    if not candidate:
        return False
    if QUESTION_MARKER_RE.match(candidate):
        return True
    # In an MCQ block, a complete question mark is a reliable boundary even
    # when the stem starts with a conditional ("If P leaves, ...?").
    if "?" in candidate:
        return True
    if QUESTION_START_RE.match(candidate):
        return True
    # Support exam-style imperative stems without a question mark.
    return bool(re.match(r"^(?:calculate|compute|find|identify|select|choose|determine)\b", candidate, re.I))


def parse_structured_quiz(text: str) -> tuple[list[dict[str, Any]], list[str], str]:
    """Parse MCQs and keep each shared Directions/passage block with its questions.

    Supports unnumbered question stems, Q1/1. stems, check-marked options,
    answer keys, multi-line explanations, and repeated Directions (01-05)
    sections. Returns (valid_questions, warnings, combined_context).
    """
    lines = _normalize_import_text(text)
    result: list[dict[str, Any]] = []
    warnings: list[str] = []
    context_lines: list[str] = []
    q_lines: list[str] = []
    options: list[str] = []
    marked_correct: list[int] = []
    answer_hint: str | None = None
    explanation_lines: list[str] = []
    in_explanation = False
    question_context = ""
    question_started = False
    section_expected: int | None = None
    section_seen = 0
    section_name = ""
    all_contexts: list[str] = []

    def check_section_count() -> None:
        full_context = "\n".join(x for x in context_lines if x.strip()).strip()
        if full_context and full_context not in all_contexts:
            all_contexts.append(full_context)
        if section_expected is not None and section_seen != section_expected:
            label = section_name or "Directions section"
            warnings.append(
                f"{label}: expected {section_expected} questions from its number range, "
                f"but detected {section_seen}. Check the imported text for a missing or malformed question."
            )

    def finish() -> None:
        nonlocal q_lines, options, marked_correct, answer_hint
        nonlocal explanation_lines, in_explanation, question_context
        nonlocal question_started, section_seen
        question = "\n".join(x for x in q_lines if x.strip()).strip()
        if not question and not options:
            return
        if question:
            section_seen += 1
        if not question:
            warnings.append("Found answer options without a question stem.")
        elif len(options) < 2:
            warnings.append(f"Skipped '{question[:55]}': fewer than two options were found.")
        elif len(options) > MAX_POLL_OPTIONS:
            warnings.append(f"Skipped '{question[:55]}': Telegram quizzes allow at most {MAX_POLL_OPTIONS} options.")
        else:
            correct_id: int | None = None
            if len(set(marked_correct)) == 1:
                correct_id = marked_correct[0]
            elif len(set(marked_correct)) > 1:
                warnings.append(f"Skipped '{question[:55]}': multiple options are marked correct.")
            if answer_hint:
                hinted = _resolve_answer(answer_hint, options)
                if hinted is not None:
                    if correct_id is not None and correct_id != hinted:
                        warnings.append(f"Skipped '{question[:55]}': tick mark and answer key disagree.")
                        correct_id = None
                    else:
                        correct_id = hinted
            if correct_id is None:
                warnings.append(f"Skipped '{question[:55]}': no clear correct answer was marked.")
            elif len({re.sub(r"\s+", " ", o).casefold() for o in options}) != len(options):
                warnings.append(f"Skipped '{question[:55]}': duplicate answer options were found.")
            else:
                explanation = " ".join(x.strip() for x in explanation_lines if x.strip()).strip()
                context = "\n".join(x for x in context_lines if x.strip()).strip()
                result.append({
                    "question": question,
                    "options": options[:],
                    "correct_id": correct_id,
                    "explanation": explanation or "Review the passage and compare each option with the stated facts.",
                    "context": context,
                })
        q_lines, options, marked_correct = [], [], []
        answer_hint = None
        explanation_lines = []
        in_explanation = False
        question_context = ""
        question_started = False

    def begin_new_section(line: str, match: re.Match[str]) -> None:
        nonlocal context_lines, section_expected, section_seen, section_name
        if options or q_lines:
            finish()
        check_section_count()
        first = match.group("first")
        last = match.group("last")
        section_expected = (int(last) - int(first) + 1) if first and last and int(last) >= int(first) else None
        section_seen = 0
        section_name = f"Directions ({first}-{last})" if first and last else "Directions section"
        body = (match.group("body") or "").strip()
        cleaned_heading = f"Directions ({first}-{last}):" if first and last else "Directions:"
        context_lines = [cleaned_heading + (f" {body}" if body else "")]

    for raw in lines:
        line = raw.strip()
        if not line:
            if options and in_explanation:
                explanation_lines.append("")
            elif q_lines and not options:
                q_lines.append("")
            continue

        direction_match = DIRECTIONS_RE.match(line)
        if direction_match:
            begin_new_section(line, direction_match)
            continue

        # Metadata after options: answer key and explanation may occur in either order.
        am = ANSWER_RE.match(line)
        em = EXPLANATION_RE.match(line)
        if options and am:
            answer_hint = am.group(1).strip()
            in_explanation = False
            continue
        if options and em:
            explanation_lines.append(em.group(1).strip())
            in_explanation = True
            continue

        # Explicitly numbered question blocks.
        q_marker = QUESTION_MARKER_RE.match(line)
        if q_marker:
            if options:
                finish()
            elif q_lines:
                # Text before the first question marker is shared passage/context.
                if not question_started:
                    context_lines.extend(x for x in q_lines if x.strip())
                else:
                    warnings.append(f"Ignored incomplete question text: {' '.join(q_lines)[:60]}")
                q_lines = []
            stripped = QUESTION_MARKER_RE.sub("", line, count=1).strip()
            if stripped:
                q_lines.append(stripped)
            question_context = "\n".join(context_lines).strip()
            question_started = True
            continue

        option_match = OPTION_RE.match(line)
        if option_match:
            if not q_lines and not options:
                # A labelled list before the first question is passage content,
                # not an MCQ option block.
                context_lines.append(line)
                continue
            in_explanation = False
            raw_option = option_match.group(4).strip()
            clean_option, marked = _strip_marker(raw_option)
            if clean_option:
                options.append(clean_option)
                if marked:
                    marked_correct.append(len(options) - 1)
            continue

        if options:
            # A new question begins only after the prior option set is complete.
            if ("?" in line or _is_question_stem(line)) and not in_explanation:
                finish()
                q_lines.append(line)
                question_context = "\n".join(context_lines).strip()
                question_started = True
                continue
            if in_explanation:
                explanation_lines.append(line)
            else:
                # Multi-line option labels are kept attached to the last option.
                if options:
                    options[-1] = f"{options[-1]} {line}".strip()
            continue

        if not question_started:
            if _is_question_stem(line, has_context=bool(context_lines)):
                q_lines.append(line)
                question_context = "\n".join(context_lines).strip()
                question_started = True
            else:
                # Directions and supporting passage text are accumulated until
                # the first question, then retained for the whole section.
                context_lines.append(line)
        else:
            # Wrapped question stems continue until the first option line.
            q_lines.append(line)

    if options or q_lines:
        if options or question_started:
            finish()
        elif q_lines:
            context_lines.extend(q_lines)

    check_section_count()
    combined_context = "\n\n".join(x for x in all_contexts if x).strip()
    return result, warnings, combined_context


def _split_sentences(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text).strip()
    # Preserve common abbreviations and initials during sentence splitting.
    cleaned = re.sub(r"\b(e\.g|i\.e|Mr|Mrs|Dr|Prof| vs|No)\.", lambda m: m.group(0).replace(".", "<DOT>"), cleaned, flags=re.I)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(])", cleaned)
    return [p.replace("<DOT>", ".").strip(" \t\n") for p in parts if len(p.strip()) >= 20]


def _make_options(answer: str, pool: list[str], kind: str, count: int = 4) -> list[str]:
    answer = answer.strip().rstrip(".,;:")
    candidates: list[str] = []
    for item in pool:
        item = item.strip().rstrip(".,;:")
        if not item or item.casefold() == answer.casefold():
            continue
        if kind == "number":
            item_value = int(item) if re.fullmatch(r"\d{1,4}", item) else NUMBER_WORDS.get(item.casefold())
            answer_value = int(answer) if re.fullmatch(r"\d{1,4}", answer) else NUMBER_WORDS.get(answer.casefold())
            if item_value is not None and answer_value is not None and abs(item_value - answer_value) <= max(10, answer_value * 2):
                candidates.append(item)
        elif kind == "date":
            if re.search(r"\b(?:18|19|20|21)\d{2}\b|\b\d{1,2}\s+[A-Z][a-z]+\s+\d{4}\b", item):
                candidates.append(item)
        elif kind == "term":
            if len(item) <= 100:
                candidates.append(item)
    candidates = list(dict.fromkeys(candidates))

    # When dates are isolated in a passage, create near-year distractors rather than
    # abandoning an otherwise clear date fact.
    if kind == "date":
        year_match = re.search(r"(18|19|20|21)\d{2}", answer)
        if year_match:
            year = int(year_match.group(0))
            if answer.isdigit():
                variants = [str(year + 1), str(year - 1), str(year + 5), str(year - 5), str(year + 10), str(year - 10)]
            else:
                variants = [answer.replace(str(year), str(year + delta)) for delta in (1, -1, 2, -2, 5, -5)]
            candidates.extend(x for x in variants if x != answer)

    candidates = list(dict.fromkeys(candidates))
    random_seed = sum(ord(c) for c in answer)
    candidates.sort(key=lambda x: (abs(len(x) - len(answer)), (sum(ord(c) for c in x) + random_seed) % 97))
    distractors = candidates[:count - 1]
    if kind == "number" and len(distractors) < count - 1:
        value = int(answer) if re.fullmatch(r"\d{1,4}", answer) else NUMBER_WORDS.get(answer.casefold())
        if value is not None:
            word_labels = {v: k for k, v in NUMBER_WORDS.items()}
            for delta in (1, -1, 2, -2, 3, -3, 5, -5, 10, -10):
                new_value = max(0, value + delta)
                candidate = word_labels.get(new_value, str(new_value)) if answer.casefold() in NUMBER_WORDS else str(new_value)
                if candidate.casefold() != answer.casefold() and candidate not in distractors:
                    distractors.append(candidate)
                if len(distractors) >= count - 1:
                    break
    if len(distractors) < count - 1:
        return []
    return [answer] + distractors[:count - 1]


def generate_extractable_questions(text: str, limit: int = 10) -> list[dict[str, Any]]:
    """Conservative offline generation from high-confidence sentence patterns."""
    sentences = _split_sentences(text)
    facts: list[tuple[str, str, str, str, str]] = []
    date_values: list[str] = []
    number_values: list[str] = []
    terms: list[str] = []
    last_reference = ""
    for sentence in sentences:
        for date_match in re.finditer(r"\b(?:18|19|20|21)\d{2}\b|\b\d{1,2}\s+[A-Z][a-z]+\s+\d{4}\b", sentence):
            date_values.append(date_match.group(0))
        for number_match in re.finditer(r"\b\d{1,4}\b", sentence):
            number_values.append(number_match.group(0))
        for number_match in re.finditer(rf"\b(?:{NUMBER_WORD_PATTERN})\b", sentence, flags=re.I):
            number_values.append(number_match.group(0).lower())
        # Acronyms followed by full form, e.g. National Sample Survey (NSS).
        acronym = re.search(r"([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,5})\s*\(([A-Z]{2,10})\)", sentence)
        if acronym:
            term = acronym.group(2)
            definition = acronym.group(1)
            terms.append(definition)
            facts.append(("acronym", term, definition, sentence, f"What does {term} stand for?"))

        # Established/founded/launched/adopted events with a clear date.
        event = re.search(
            r"(?P<subject>[A-Z][A-Za-z0-9'’&().\-]*(?:\s+[A-Z][A-Za-z0-9'’&().\-]*){0,5})\s+"
            r"(?P<verb>was established|was founded|was launched|was introduced|was adopted|was passed|was signed|began|started|occurred)\s+"
            r"(?:on\s+)?(?P<date>(?:\d{1,2}\s+[A-Z][a-z]+\s+\d{4}|\b(?:18|19|20|21)\d{2}\b))",
            sentence,
        )
        if event:
            subj, verb, date = event.group("subject", "verb", "date")
            subj = re.sub(r"^The\s+", "the ", subj)
            last_reference = subj
            if verb in ("was established", "was founded", "was launched", "was introduced", "was adopted", "was passed", "was signed"):
                prompt = f"When was {subj} {verb.split(' ', 1)[1]}?"
            else:
                action = "begin" if verb in ("began", "started") else "occur"
                prompt = f"When did {subj} {action}?"
            facts.append(("date", subj, date, sentence, prompt))

        # Numerical counts.
        count_match = re.search(
            r"(?P<subject>(?:The\s+)?[A-Z][A-Za-z0-9'’&().\-]*(?:\s+[A-Za-z][A-Za-z0-9'’&().\-]*){0,5})\s+"
            rf"(?:has|have|had|comprises|contains|consists of)\s+(?P<number>\d{{1,4}}|(?:{NUMBER_WORD_PATTERN}))\s+(?P<unit>[A-Za-z][A-Za-z -]{{1,45}})",
            sentence,
        )
        if count_match:
            subj = count_match.group("subject")
            if subj.casefold() in {"it", "they", "he", "she", "this", "these", "those"} and last_reference:
                subj = last_reference
            number = count_match.group("number")
            unit = count_match.group("unit").strip()
            facts.append(("number", subj, number, sentence, f"According to the passage, how many {unit} does {subj} have?"))

        # Explicit definitions / classifications. Restrict to short subjects and verb phrases.
        definition = re.search(
            r"^(?P<subject>(?:The\s+)?[A-Z][A-Za-z0-9'’&().\-]*(?:\s+[A-Za-z][A-Za-z0-9'’&().\-]*){0,4})\s+"
            r"(?P<verb>is known as|is called|refers to|means|is|was)\s+(?P<answer>[^,;.!?]{5,100})[.!?]?$",
            sentence,
        )
        if definition:
            subject = definition.group("subject").strip()
            answer = definition.group("answer").strip()
            verb = definition.group("verb")
            if not re.fullmatch(r"(?:a|an|the|it|this|that|there)", subject, re.I):
                if verb == "means":
                    prompt = f"According to the passage, what does {subject} mean?"
                elif verb == "refers to":
                    prompt = f"According to the passage, what does {subject} refer to?"
                elif verb == "is called":
                    prompt = f"According to the passage, what is {subject} called?"
                elif verb == "is known as":
                    prompt = f"According to the passage, what is {subject} known as?"
                else:
                    prompt = f"According to the passage, what is {subject}?"
                facts.append(("term", subject, answer, sentence, prompt))

        # Capitalized "X is the capital of Y" relation.
        capital = re.search(r"(?P<place>[A-Z][A-Za-z .'-]{1,45})\s+is the capital of\s+(?P<country>[A-Z][A-Za-z .'-]{1,45})", sentence)
        if capital:
            facts.append(("relation", capital.group("country").strip(), capital.group("place").strip(), sentence,
                          f"Which city is identified as the capital of {capital.group('country').strip()}?"))

    generated: list[dict[str, Any]] = []
    used: set[str] = set()
    used_evidence: set[str] = set()
    for kind, subject, answer, evidence, question in facts:
        key = re.sub(r"\W+", " ", question).casefold()
        evidence_key = re.sub(r"\s+", " ", evidence).strip().casefold()
        if key in used or evidence_key in used_evidence:
            continue
        used.add(key)
        used_evidence.add(evidence_key)
        if kind == "number":
            pool = number_values
            option_kind = "number"
        elif kind == "date":
            pool = date_values
            option_kind = "date"
        elif kind in ("term", "acronym", "relation"):
            pool = [f[2] for f in facts if f[0] == kind]
            option_kind = "term"
        else:
            continue
        options = _make_options(answer, pool, option_kind)
        if not options and kind in ("term", "acronym", "relation"):
            # A two-choice true/false item remains grounded when the paragraph
            # contains only one definition or named relationship.
            if kind == "acronym":
                statement = f"According to the paragraph, {subject} stands for {answer}."
            elif kind == "relation":
                statement = f"According to the paragraph, {answer} is the capital of {subject}."
            else:
                statement = evidence
            question = f"True or false: {statement}"
            options = ["True", "False"]
        if not options:
            continue
        options = list(dict.fromkeys(options))
        if len(options) < 2:
            continue
        # Mix answer position without changing the verified option set.
        correct_id = (sum(ord(c) for c in answer) + len(generated)) % len(options)
        if options == ["True", "False"]:
            correct_id = 0
        else:
            options.insert(correct_id, options.pop(0))
        generated.append({
            "question": question[:300],
            "options": options,
            "correct_id": correct_id,
            "explanation": f"The passage states: {evidence}"[:200],
            "context": "",
            "evidence": evidence,
        })
        if len(generated) >= limit:
            break
    return generated


def _normalize_source(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def validate_ai_quiz(payload: Any, source_text: str, limit: int = 20) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        items = payload.get("questions")
    else:
        items = payload
    if not isinstance(items, list):
        raise ValueError("AI response did not contain a questions list.")
    source_normalized = _normalize_source(source_text)
    output: list[dict[str, Any]] = []
    seen: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question", "")).strip()
        options = item.get("options")
        answer = str(item.get("answer", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        explanation = str(item.get("explanation", "")).strip()
        if not question or len(question) > 300 or not isinstance(options, list) or not 2 <= len(options) <= MAX_POLL_OPTIONS:
            continue
        options = [str(x).strip() for x in options]
        if any(not x or len(x) > 100 for x in options):
            continue
        normalized_options = [re.sub(r"\s+", " ", x).casefold() for x in options]
        if len(set(normalized_options)) != len(options):
            continue
        try:
            correct_id = normalized_options.index(re.sub(r"\s+", " ", answer).casefold())
        except ValueError:
            continue
        if not evidence or _normalize_source(evidence) not in source_normalized:
            continue
        evidence_normalized = _normalize_source(evidence)
        if _normalize_source(answer) not in evidence_normalized:
            continue
        q_key = re.sub(r"\W+", " ", question).casefold()
        if any(difflib.SequenceMatcher(None, q_key, prev).ratio() > 0.90 for prev in seen):
            continue
        seen.append(q_key)
        if not explanation:
            explanation = f"The source states: {evidence}"
        if len(explanation) > 200:
            explanation = explanation[:197].rstrip() + "..."
        output.append({
            "question": question,
            "options": options,
            "correct_id": correct_id,
            "explanation": explanation,
            "context": "",
            "evidence": evidence,
        })
        if len(output) >= limit:
            break
    if not output:
        raise ValueError("The paragraph did not produce any questions that passed source-evidence checks.")
    return output



def create_quiz_from_text(text: str) -> tuple[list[dict[str, Any]], list[str], str, str]:
    """Synchronous structured parser and offline fallback."""
    quizzes, warnings, context = parse_structured_quiz(text)
    if quizzes:
        return quizzes, warnings, context, "structured"
    fallback = generate_extractable_questions(text)
    if fallback:
        return fallback, warnings, "", "extractive"
    return [], warnings, context, "unrecognized"


def questions_to_text(questions: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    previous_context: str | None = None
    for index, item in enumerate(questions, 1):
        current_context = str(item.get("context", "")).strip()
        if current_context and current_context != previous_context:
            if lines:
                lines.append("")
            lines.extend([current_context, ""])
        previous_context = current_context
        lines.append(f"{index}. {item['question']}")
        for option_index, option in enumerate(item["options"]):
            marker = " ✅" if option_index == item["correct_id"] else ""
            lines.append(f"{chr(65 + option_index)}) {option}{marker}")
        if item.get("explanation"):
            lines.append(f"Explanation: {item['explanation']}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"

