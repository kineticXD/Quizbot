"""Text helpers which do not depend on any Telegram framework package."""
from __future__ import annotations


def split_telegram_text(text: str, limit: int = 3900) -> list[str]:
    """Split plain text below Telegram's 4096 UTF-16-code-unit message limit.

    A little headroom is left for Telegram entity/accounting differences.
    Paragraph and word boundaries are preferred, but a single very long word
    is still split instead of being dropped. All source characters are kept.
    """
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining.encode("utf-16-le")) // 2 <= limit:
            chunks.append(remaining)
            break
        units = 0
        end = 0
        for index, char in enumerate(remaining):
            units_for_char = 2 if ord(char) > 0xFFFF else 1
            if units + units_for_char > limit:
                break
            units += units_for_char
            end = index + 1
        if end <= 0:
            end = 1
        floor = max(0, int(end * 0.70))
        cut = max(remaining.rfind("\n", floor, end), remaining.rfind(" ", floor, end))
        if cut >= floor:
            end = cut + 1
        chunks.append(remaining[:end])
        remaining = remaining[end:]
    return [chunk for chunk in chunks if chunk]
