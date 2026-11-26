"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import re
import unicodedata as _ud
from typing import Optional

_LATEX_MAP_NOT_USED = None  # (LaTeX/markdown cleanup lives in shared.utils.text)


def _is_emoji(char: str) -> bool:
    """Return True if `char` is a single emoji (excluding the ✅ marker)."""
    if char == "✅":
        return False
    cp = ord(char)
    if 0x1F300 <= cp <= 0x1FAFF:
        return True
    if 0x2600 <= cp <= 0x26FF:
        return True
    if 0x2700 <= cp <= 0x27BF:
        return True
    if 0xFE00 <= cp <= 0xFE0F:
        return True
    if 0x1F1E0 <= cp <= 0x1F1FF:
        return True
    if 0x231A <= cp <= 0x231B:
        return True
    if 0x23E9 <= cp <= 0x23F3:
        return True
    if 0x25AA <= cp <= 0x25FE:
        return True
    if 0x2614 <= cp <= 0x2615:
        return True
    if 0x2648 <= cp <= 0x2653:
        return True
    return False


def _line_is_emoji_separator(line: str) -> bool:
    """True if the line consists only of emoji/emoji-modifier characters."""
    s = line.strip()
    if not s:
        return False
    for ch in s:
        if ch in ("️", "‍", "︎"):
            continue
        if not _is_emoji(ch):
            return False
    return True


def clean_markdown(text: str) -> str:
    """Strip common markdown emphasis/headings from pasted question text."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    return text


_TABLE_LINE_RE = re.compile(r"^\|.*\|$")


def _pad_table_blocks(text: str) -> str:
    """Ensure blank lines surround any markdown-table block so tables
    render distinctly from surrounding prose."""
    if "|" not in text:
        return text
    lines = text.split("\n")
    segments: list[tuple[str, bool]] = []
    i, n = 0, len(lines)
    while i < n:
        if _TABLE_LINE_RE.match(lines[i].strip()):
            block = []
            while i < n and _TABLE_LINE_RE.match(lines[i].strip()):
                block.append(lines[i])
                i += 1
            segments.append(("\n".join(block), True))
        else:
            segments.append((lines[i], False))
            i += 1
    if not segments:
        return text
    out = segments[0][0]
    for k in range(1, len(segments)):
        prev_is_table = segments[k - 1][1]
        cur_is_table = segments[k][1]
        sep = "\n\n" if (prev_is_table or cur_is_table) else "\n"
        out += sep + segments[k][0]
    if segments[-1][1]:
        out += "\n\n"
    return out


# Telegram quiz polls support 2–12 options (A–L). Start a block only at A)
# so Roman-numeral statements such as "I. ..." inside CLAT passages are not
# mistaken for answer choices. Detect M) as well so a 13-option question is
# kept together and rejected rather than split into a misleading 12-option item.
MIN_OPTIONS = 2
MAX_OPTIONS = 12
_OPTION_START_RE = re.compile(r"^\s*A\s*[).:]\s*\S", re.IGNORECASE)
_OPTION_LINE_RE = re.compile(r"^\s*[A-M]\s*[).:]\s*\S", re.IGNORECASE)
_QUESTION_LABEL_RE = re.compile(r"^\s*(?:(?:question|q)\s*)?\d+\s*[.):]\s*\S", re.IGNORECASE)
_QUESTION_STARTS = (
    "what ", "which ", "who ", "when ", "where ", "why ", "how ",
    "according to ", "based on ", "from the passage", "the passage suggests",
    "it can be inferred", "which of ", "what can be inferred",
    "select ", "choose ", "identify ", "the author", "the main idea",
    "the ratio ", "the number ", "the proportion ", "what percentage",
    "what fraction", "which percentage", "students taking ",
    "aspirants taking ", "those attending ", "those taking ",
    "attendees taking ", "the percentage ", "the fraction ",
)

def _is_question_prompt_line(line: str) -> bool:
    value = line.strip()
    lower = value.lower()
    return bool(_QUESTION_LABEL_RE.match(value)) or lower.startswith(_QUESTION_STARTS) or value.endswith("?")

def _extract_shared_passage(block: str) -> str:
    lines = block.split("\n")
    first_option = next((i for i, line in enumerate(lines)
                         if _OPTION_START_RE.match(line)), len(lines))
    candidates = [i for i, line in enumerate(lines[:first_option])
                  if line.strip() and _is_question_prompt_line(line)]
    if not candidates:
        return ""
    question_start = candidates[-1]
    prefix = "\n".join(lines[:question_start]).strip()
    return prefix if len(prefix) >= 40 else ""


def _inherit_shared_passage(blocks: list[str]) -> list[str]:
    """Repeat each leading passage for later questions that refer to it.

    CLAT-style sets often provide one passage followed by multiple A-L
    question blocks. Preserve that context on every parsed question, while
    allowing a later block with its own passage to start a new passage group.
    """
    if len(blocks) < 2:
        return blocks
    updated: list[str] = []
    active_passage = ""
    for block in blocks:
        own_passage = _extract_shared_passage(block)
        first = next((line for line in block.split("\n") if line.strip()), "")
        if own_passage:
            active_passage = own_passage
            updated.append(block)
        elif active_passage and _is_question_prompt_line(first):
            updated.append(f"{active_passage}\n\n{block}")
        else:
            updated.append(block)
    return updated


def split_question_blocks(text: str) -> list[str]:
    """Split pasted/TXT content without breaking blank paragraphs in passages.

    A boundary is recognised after a run of at least two labelled option lines.
    Blank lines inside a passage remain part of the same question. Legacy
    formats without labelled options retain blank-line-separated behaviour.
    """
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    lines = normalized.split("\n")
    boundaries: list[int] = []
    found_option_run = False
    i = 0
    while i < len(lines):
        if not _OPTION_START_RE.match(lines[i]):
            i += 1
            continue

        option_count = 0
        last_option = i
        j = i
        while j < len(lines):
            if _OPTION_LINE_RE.match(lines[j]):
                option_count += 1
                last_option = j
                j += 1
                continue
            if not lines[j].strip():
                j += 1
                continue
            if lines[j].strip().lower().startswith(("ex:", "explanation:", "rt:", "id:")):
                j += 1
                continue
            break

        if option_count >= 2:
            found_option_run = True
            # Keep blank lines and explanation/metadata with the prior question.
            k = last_option + 1
            while k < len(lines) and (
                not lines[k].strip()
                or lines[k].strip().lower().startswith(("ex:", "explanation:", "rt:", "id:"))
            ):
                k += 1
            if k < len(lines):
                boundaries.append(k)
                i = k
                continue
        i += 1

    if not boundaries:
        if found_option_run:
            return [normalized]
        emoji_separators = sum(1 for line in lines if _line_is_emoji_separator(line))
        if emoji_separators == 1:
            return [normalized]
        legacy_blocks = [part.strip() for part in re.split(r"\n[ \t]*\n+", normalized) if part.strip()]
        return _inherit_shared_passage(legacy_blocks)

    starts = [0, *boundaries]
    ends = [*boundaries, len(lines)]
    parsed_blocks = ["\n".join(lines[a:b]).strip() for a, b in zip(starts, ends)
                     if "\n".join(lines[a:b]).strip()]
    return _inherit_shared_passage(parsed_blocks)



def parse_question_block(blk: str) -> Optional[dict]:
    """Parse one question block into a question plus optional shared context.

    A CLAT-style passage/caselet is returned once as ``context`` instead of
    being copied into every question's stem. ``_inherit_shared_passage`` may
    prepend the active passage to later blocks before this function is called;
    this function extracts that prefix again so each saved question has a
    clean, individual prompt.

    `correct_option_id` is an int (single ✅) or a list[int] (multiple ✅).
    Returns None if the block doesn't parse into a valid question.

    Newlines inside the question text (e.g. a passage followed by a blank
    line, followed by the actual question) are preserved exactly -- only
    the separator/option/marker lines used to structurally locate the
    options are stripped away, never the blank lines a user typed on
    purpose for spacing within the question itself.
    """
    blk = clean_markdown(blk)
    context = _extract_shared_passage(blk)
    if context:
        # The parser's splitter temporarily inherits the active caselet into
        # each block so that older import formats keep their context. Store it
        # in its own field and leave only the individual prompt in question.
        prefix = context
        if blk.startswith(prefix):
            blk = blk[len(prefix):].lstrip("\n")
    # Keep blank lines here (structure-preserving) -- they're only used
    # for splitting on the caller side (blocks separated by "\n\n"); a
    # blank line *inside* one block is an intentional paragraph break in
    # the question and must survive into the final `question` string.
    all_lines = blk.split("\n")
    if not any(ln.strip() for ln in all_lines):
        return None

    exp = None
    filtered = []
    for ln in all_lines:
        if ln.strip().startswith("Ex:"):
            exp = ln.strip()[3:].strip()
        else:
            filtered.append(ln)
    all_lines = filtered

    # Structural scan (separator / option markers) only ever needs to look at
    # non-blank lines, but indexes must map back into `all_lines` so the
    # split point doesn't chop a preserved blank line in half.
    non_blank_idx = [i for i, ln in enumerate(all_lines) if ln.strip()]
    if not non_blank_idx:
        return None

    sep_line_idx = None
    for i in non_blank_idx:
        if _line_is_emoji_separator(all_lines[i]):
            sep_line_idx = i
            break

    option_line_idx = None
    for i in non_blank_idx:
        if _OPTION_START_RE.match(all_lines[i].strip()):
            option_line_idx = i
            break

    first_nonblank = non_blank_idx[0]
    if sep_line_idx is not None:
        q_lines = all_lines[:sep_line_idx]
        opt_lines = all_lines[sep_line_idx + 1:]
    elif option_line_idx is not None and option_line_idx > first_nonblank:
        q_lines = all_lines[:option_line_idx]
        opt_lines = all_lines[option_line_idx:]
    else:
        # No structural marker found -- fall back to "first non-blank line
        # is the question, everything after is options" (matches the
        # original single-line-question behaviour).
        q_lines = all_lines[: first_nonblank + 1]
        opt_lines = all_lines[first_nonblank + 1:]

    # Trim only leading/trailing wholly-blank lines from the question (so
    # e.g. a stray blank line right before the separator doesn't leave a
    # trailing "\n"), but keep every blank line that falls *between* two
    # real content lines -- that's the intentional paragraph break.
    while q_lines and not q_lines[0].strip():
        q_lines = q_lines[1:]
    while q_lines and not q_lines[-1].strip():
        q_lines = q_lines[:-1]
    question = _pad_table_blocks("\n".join(q_lines))

    opts: list[str] = []
    coids: list[int] = []
    for ln in opt_lines:
        if not ln.strip():
            continue
        ln = ln.strip()
        ln = re.sub(r"^[A-La-l]\s*[).:]\s*", "", ln)
        if "✅" in ln:
            coids.append(len(opts))
            ln = ln.replace("✅", "").strip()
        opts.append(ln)

    if not question or not (MIN_OPTIONS <= len(opts) <= MAX_OPTIONS) or not coids:
        return None

    coid = coids[0] if len(coids) == 1 else coids
    return {
        "question": question,
        "context": context or None,
        "options": opts,
        "correct_option_id": coid,
        "explanation": exp,
    }


def filter_words(text: Optional[str], remove_words: list[str]) -> Optional[str]:
    """Strip `[n/m]` progress markers and any user-configured remove-words
    from `text`, while preserving newlines exactly as typed/pasted.

    Only horizontal whitespace (spaces/tabs) is collapsed -- line breaks
    are never touched, so multi-line questions/options/explanations keep
    their original line structure end-to-end.
    """
    if not text:
        return text
    text = re.sub(r"\[\s*\d+\s*/\s*\d+\s*\]", "", text)
    if remove_words:
        for w in remove_words:
            text = re.sub(rf"\b{re.escape(w)}\b", "", text, flags=re.IGNORECASE)
    # Collapse runs of spaces/tabs only (never \n) and trim horizontal
    # whitespace at the start/end of each line, without merging lines.
    lines = [re.sub(r"[ \t]+", " ", ln).strip(" \t") for ln in text.split("\n")]
    return "\n".join(lines).strip("\n")


def strip_source_noise(text: Optional[str]) -> Optional[str]:
    """Remove leaked `[Q 3/10]`-style progress markers, raw URLs, t.me
    links, and @mentions that sometimes end up pasted into quiz text.

    The bracket/paren marker only matches when it actually looks like a
    progress tag -- either a `Q` prefix (`[Q3]`, `(Q.5)`) or a `n/m` pair
    (`[11/100]`, `(3/10)`). A bare lone number in brackets/parens, like
    `(1)` or `[2]`, is legitimate question content (e.g. an enumerated
    list: "(1) Anaphase (2) Metaphase") and must never be stripped.
    """
    if not text:
        return text
    pattern = (
        r"(?:[\[\(]\s*Q\.?\s*\d+(?:\s*/\s*\d+)?\s*[\]\)]|"
        r"[\[\(]\s*\d+\s*/\s*\d+\s*[\]\)]|"
        r"\bQ\.?\s*\d+\s*/\s*\d+\)?|https?://[^\s]+|t\.me/[^\s]+|@\w+)"
    )
    return re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
