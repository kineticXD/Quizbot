"""Forum-topic destination resolution for Shikimori poll publishing."""
from __future__ import annotations

from typing import Any, Optional


def resolve_poll_destination(
    invocation_chat_id: int,
    invocation_chat_type: str,
    current_thread_id: Optional[int],
    settings: dict[str, Any],
) -> tuple[int, Optional[int], str]:
    """Resolve /pollquiz destination from current topic or saved user target.

    Running the command inside a topic always wins. From the group's General
    chat or a private chat, the user's saved /polltarget is used when it
    matches the current group, or when the command is sent privately.
    """
    if current_thread_id is not None:
        return int(invocation_chat_id), int(current_thread_id), "current_topic"

    saved_chat_id = settings.get("poll_target_chat_id")
    saved_thread_id = settings.get("poll_target_thread_id")
    if saved_chat_id is not None and saved_thread_id is not None:
        saved_chat_id = int(saved_chat_id)
        saved_thread_id = int(saved_thread_id)
        if str(invocation_chat_type) == "private" or saved_chat_id == int(invocation_chat_id):
            return saved_chat_id, saved_thread_id, "saved_topic"

    return int(invocation_chat_id), None, "current_chat"
