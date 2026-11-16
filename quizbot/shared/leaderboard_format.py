"""Shared rich and plain leaderboard formatting for Telegram quiz results.

Accuracy is deliberately calculated over attempted questions only:
correct / (correct + wrong). Unanswered questions do not reduce accuracy.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _as_float(value: Any) -> float:
    try:
        number = float(value or 0)
        return number if number == number and abs(number) != float("inf") else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def accuracy_percent(correct: Any, wrong: Any) -> float | None:
    """Return accuracy among attempted answers, or None when none were attempted."""
    c = _as_int(correct)
    w = _as_int(wrong)
    attempted = c + w
    return (c / attempted * 100.0) if attempted else None


def _clean_name(value: Any, limit: int = 28) -> str:
    name = re.sub(r"\s+", " ", str(value or "Aspirant")).strip()
    if len(name) > limit:
        name = name[: limit - 1].rstrip() + "…"
    return name or "Aspirant"


def _markdown_cell(value: Any) -> str:
    """Escape user-controlled names so they cannot break the Markdown table."""
    text = _clean_name(value)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#+.!|])", r"\\\1", text).replace("\n", " ")


def format_duration(seconds: Any) -> str:
    total = _as_int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


def normalise_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows or []:
        correct = _as_int(row.get("correct"))
        wrong = _as_int(row.get("wrong"))
        attempted = _as_int(row.get("answered", correct + wrong))
        # If legacy records have no separate answered field, correct+wrong is authoritative.
        if "answered" not in row:
            attempted = correct + wrong
        accuracy = accuracy_percent(correct, wrong)
        result.append({
            "name": _clean_name(row.get("name") or row.get("user_name") or row.get("username")),
            "correct": correct,
            "wrong": wrong,
            "attempted": attempted,
            "score": _as_float(row.get("score")),
            "time": _as_float(row.get("total_time", row.get("time_taken", 0))),
            "accuracy": accuracy,
        })
    return result


def _rank_label(rank: int) -> str:
    return _MEDALS.get(rank, str(rank))


def format_leaderboard_markdown(
    title: str,
    rows: Iterable[dict[str, Any]],
    *,
    total_questions: int = 0,
    status: str = "Final Results",
    subtitle: str | None = None,
    limit: int = 20,
) -> str:
    """Build the rich Markdown table used by Telegram's sendRichMessage API."""
    values = normalise_rows(rows)
    total = _as_int(total_questions)
    shown = values[: max(1, int(limit))]
    heading = _markdown_cell(title)
    participant_label = f"{len(values)} participant" + ("s" if len(values) != 1 else "")
    meta = subtitle or " · ".join(part for part in (
        status,
        f"{total} questions" if total else None,
        participant_label,
    ) if part)

    lines = [
        f"### 🏆 {heading}",
        f"**Advance Quiz Bot**  ·  **{_markdown_cell(status)}**",
        f"_{_markdown_cell(meta)}_",
        "",
        "| # | Name | ✅ | ❌ | Score | Time | Accuracy |",
        "|---:|:---|---:|---:|---:|---:|---:|",
    ]
    if not shown:
        lines.append("| — | No participants | — | — | — | — | — |")
    else:
        for rank, row in enumerate(shown, 1):
            acc = f"{row['accuracy']:.1f}%" if row["accuracy"] is not None else "—"
            lines.append(
                f"| {_rank_label(rank)} | {_markdown_cell(row['name'])} | "
                f"{row['correct']} | {row['wrong']} | {row['score']:.2f} | "
                f"{format_duration(row['time'])} | {acc} |"
            )
    if len(values) > len(shown):
        lines.append("")
        lines.append(f"_Showing top {len(shown)} of {len(values)} participants._")
    lines.extend(["", "_Accuracy = correct answers ÷ attempted answers. Unanswered questions are excluded._"])
    return "\n".join(lines)


def format_leaderboard_plain(
    title: str,
    rows: Iterable[dict[str, Any]],
    *,
    total_questions: int = 0,
    status: str = "Final Results",
    limit: int = 20,
) -> str:
    """Readable fallback if the Telegram rich-message API is unavailable."""
    values = normalise_rows(rows)
    total = _as_int(total_questions)
    lines = [f"🏆 {title}", f"Advance Quiz Bot · {status}", f"{total} questions · {len(values)} participants", ""]
    for rank, row in enumerate(values[: max(1, int(limit))], 1):
        acc = f"{row['accuracy']:.1f}%" if row["accuracy"] is not None else "—"
        lines.append(
            f"{_rank_label(rank)} {row['name']}\n"
            f"✅ {row['correct']}   ❌ {row['wrong']}   Score {row['score']:.2f}\n"
            f"⏱ {format_duration(row['time'])}   Accuracy {acc}"
        )
    if not values:
        lines.append("No participant answers have been recorded.")
    if len(values) > limit:
        lines.append(f"\nShowing top {limit} of {len(values)} participants.")
    lines.append("\nAccuracy = correct ÷ attempted answers; unanswered questions are excluded.")
    return "\n\n".join(lines)
