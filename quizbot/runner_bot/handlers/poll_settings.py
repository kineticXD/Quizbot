"""Per-user defaults for the timed Telegram poll-quiz mode."""
from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from quizbot.database import PollQuizSettingsRepository, get_db
from quizbot.shared import config

logger = logging.getLogger(__name__)

_TIMER_CHOICES = (
    (0, "Global default"),
    (600, "10 min"),
    (1800, "30 min"),
    (3600, "1 hour"),
    (10800, "3 hours"),
    (43200, "12 hours"),
    (86400, "24 hours"),
)


def _duration(seconds: int) -> str:
    if not seconds:
        return f"Global ({config.POLLQUIZ_AUTO_CLOSE_SECONDS // 60} min)"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} hour(s)"
    return f"{seconds // 60} min"


def _keyboard(user_id: int, settings: dict[str, Any]) -> InlineKeyboardMarkup:
    def toggle(field: str, label: str, key: str) -> InlineKeyboardButton:
        value = bool(settings.get(key))
        state = "ON ✅" if value else "OFF ❌"
        return InlineKeyboardButton(f"{label}: {state}", callback_data=f"pollset:{user_id}:{field}:{int(not value)}")

    timer = int(settings.get("timer_seconds", 0) or 0)
    rows = [
        [toggle("anonymous", "Anonymous polls", "anonymous")],
        [toggle("shuffle_questions", "Shuffle questions", "shuffle_questions")],
        [toggle("shuffle_options", "Shuffle options", "shuffle_options")],
        [toggle("scoreboard", "Final leaderboard", "scoreboard")],
        [InlineKeyboardButton(f"⏱ Timer: {_duration(timer)}", callback_data=f"pollset:{user_id}:noop:0")],
    ]
    timer_buttons = []
    for seconds, label in _TIMER_CHOICES:
        prefix = "✓ " if timer == seconds else ""
        timer_buttons.append(InlineKeyboardButton(prefix + label, callback_data=f"pollset:{user_id}:timer_seconds:{seconds}"))
    rows.extend([timer_buttons[:3], timer_buttons[3:]])
    return InlineKeyboardMarkup(rows)


async def _show(update_or_query, user_id: int, settings: dict[str, Any], edit: bool = False) -> None:
    text = (
        "⚙️ <b>Timed Poll Quiz Settings</b>\n\n"
        f"• Anonymous: <b>{'On' if settings.get('anonymous') else 'Off'}</b>\n"
        f"• Shuffle questions: <b>{'On' if settings.get('shuffle_questions') else 'Off'}</b>\n"
        f"• Shuffle options: <b>{'On' if settings.get('shuffle_options') else 'Off'}</b>\n"
        f"• Final leaderboard: <b>{'On' if settings.get('scoreboard', True) else 'Off'}</b>\n"
        f"• Auto-close: <b>{_duration(int(settings.get('timer_seconds', 0) or 0))}</b>\n\n"
        "These are your defaults when you run <code>/pollquiz QUIZ_ID</code>. "
        "A duration entered directly in the command overrides this timer. "
        "Anonymous polls cannot produce individual scores; channels are anonymous by Telegram design."
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
    if field == "noop":
        return
    if field in {"anonymous", "shuffle_questions", "shuffle_options", "scoreboard"}:
        if raw_value not in ("0", "1"):
            return
        value: Any = raw_value == "1"
    elif field == "timer_seconds":
        try:
            value = int(raw_value)
        except ValueError:
            return
        if value not in {seconds for seconds, _ in _TIMER_CHOICES}:
            return
    else:
        return

    repo = PollQuizSettingsRepository(get_db())
    await repo.update(target_user, **{field: value})
    settings = await repo.get(target_user)
    await _show(query, target_user, settings, edit=True)


def register(application: Application) -> None:
    application.add_handler(CommandHandler("pollsettings", pollsettings_command))
    application.add_handler(CallbackQueryHandler(pollsettings_callback, pattern=r"^pollset:"))
