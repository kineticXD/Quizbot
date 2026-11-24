"""Small pure helpers for parsing callback ownership IDs."""
from __future__ import annotations


def batch_callback_target_user_id(parts: list[str], action: str) -> int:
    """Return the user ID embedded in a batch callback payload.

    Most payloads end with the user ID. ``editfield`` places the field name
    after it: bb:editfield:<batch_id>:<user_id>:<field>.
    """
    index = -2 if action == "editfield" else -1
    return int(parts[index])
