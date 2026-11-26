"""Per-user defaults for persistent Telegram poll-quiz runs."""
from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from quizbot.database import PollQuizSettingsRepository, get_db

logger = logging.getLogger(__name__)


def _keyboard(user_id: int, settings: dict[str, Any]) -> InlineKeyboardMarkup:
    def toggle(field: str, label: str, key: str) -> InlineKeyboardButton:
        value = bool(settings.get(key))
        state = "ON ✅" if value else "OFF ❌"
        return InlineKeyboardButton(
            f"{label}: {state}",
            callback_data=f"pollset:{user_id}:{field}:{int(not value)}",
        )

    return InlineKeyboardMarkup([
        [toggle("anonymous", "Anonymous polls", "anonymous")],
        [toggle("shuffle_questions", "Shuffle questions", "shuffle_questions")],
        [toggle("shuffle_options", "Shuffle options", "shuffle_options")],
        [toggle("scoreboard", "Live/final leaderboard", "scoreboard")],
        [InlineKeyboardButton("🏠 Shikimori home", callback_data="shk:home")],
    ])


async def _show(update_or_query, user_id: int, settings: dict[str, Any], edit: bool = False) -> None:
    text = (
        "⚙️ <b>Shikimori · Poll Studio</b>\n\n"
        f"• Anonymous: <b>{'On' if settings.get('anonymous') else 'Off'}</b>\n"
        f"• Shuffle questions: <b>{'On' if settings.get('shuffle_questions') else 'Off'}</b>\n"
        f"• Shuffle options: <b>{'On' if settings.get('shuffle_options') else 'Off'}</b>\n"
        f"• Final leaderboard: <b>{'On' if settings.get('scoreboard', True) else 'Off'}</b>\n\n"
        "♾️ <b>Poll lifetime: Unlimited</b>\n"
        "Polls do not have an automatic closing timer. They remain open until an administrator uses "
        "<code>/pollclose</code> or <code>/pollfinish</code>. Use <code>/pollstop</code> only to stop sending more questions. You can forward the polls to other chats.\n\n"
        "Anonymous polls cannot produce individual scores. Telegram channels only support anonymous polls."
    )
    markup = _keyboard(user_id, settings)
    if edit:
        await update_or_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await update_or_query.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def pollsettings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    repo = PollQuizSettingsRepository(get_db())
    settings = await repo.get(user_id)
    await _show(update, user_id, settings)


async def pollsettings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = (query.data or "").split(":")
    if len(parts) != 4:
        await query.answer()
        return
    try:
        target_user = int(parts[1])
    except ValueError:
        await query.answer()
        return
    if query.from_user.id != target_user:
        await query.answer("These settings belong to another user.", show_alert=True)
        return

    await query.answer()
    field, raw_value = parts[2], parts[3]
    # Timer callbacks from older keyboards are intentionally ignored.
    if field not in {"anonymous", "shuffle_questions", "shuffle_options", "scoreboard"}:
        return
    if raw_value not in ("0", "1"):
        return

    repo = PollQuizSettingsRepository(get_db())
    await repo.update(target_user, **{field: raw_value == "1"})
    settings = await repo.get(target_user)
    await _show(query, target_user, settings, edit=True)


def register(application: Application) -> None:
    application.add_handler(CommandHandler("pollsettings", pollsettings_command))
    application.add_handler(CallbackQueryHandler(pollsettings_callback, pattern=r"^pollset:"))
