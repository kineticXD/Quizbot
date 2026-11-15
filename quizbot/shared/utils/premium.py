"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
"""

from __future__ import annotations

from quizbot.database import UserRepository, get_db
from quizbot.shared import config


def _is_owner_or_admin(user_id: int) -> bool:
    """Return whether *user_id* is configured as the bot owner or an admin.

    Owner/admin access is a deployment-level privilege and must not depend on
    a MongoDB premium subscription document being present.
    """
    return (
        (config.OWNER_ID is not None and int(user_id) == config.OWNER_ID)
        or int(user_id) in config.ADMIN_IDS
    )


async def is_premium_user(user_id: int) -> bool:
    """Return whether a user can use premium-gated features.

    Access is granted to all users when FREE_BOT is enabled, to the configured
    owner/admin accounts, or to users with an active database subscription.
    This makes the OWNER_ID / ADMIN_IDS settings consistently effective for
    every premium-gated command (including /create, /myquizzes and /edit).
    """
    uid = int(user_id)
    if config.FREE_BOT or _is_owner_or_admin(uid):
        return True

    repo = UserRepository(get_db())
    return await repo.is_premium(uid)


async def grant_premium(user_id: int, days: int | None = 30) -> None:
    """Grant premium to a user for `days` days, or permanently if days is None."""
    repo = UserRepository(get_db())
    await repo.set_premium(user_id, days)


async def revoke_premium(user_id: int) -> None:
    repo = UserRepository(get_db())
    await repo.revoke_premium(user_id)
