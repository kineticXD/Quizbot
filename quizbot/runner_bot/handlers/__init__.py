"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

from telegram.ext import Application

from . import admin, ai_quiz, creator, creator_admin, creator_batches, creator_reports, mix, pdf_quiz, poll_quiz, poll_settings, quiz_play, reports, scheduling, setup_wizard, translation

_MODULES = (
    quiz_play,     # /start, /skip, /pause, /resume, /stop, /leaderboard, timer controls, poll answers
    setup_wizard,  # qs_* quiz-setup wizard callbacks
    poll_quiz,     # /pollquiz, /pollstop
    poll_settings, # /pollsettings per-user permanent-poll preferences
    mix,           # /mix
    ai_quiz,       # /aiquiz
    pdf_quiz,      # /pdfquiz
    reports,       # /html, /pdf, compare_ callback
    scheduling,    # /schedule, /viewschedule, /cancelschedule
    translation,   # /trans
    creator,       # Bot API-native quiz creation, editing, imports, settings, AI keys and payments
    creator_batches, # Bot API-native batch management
    creator_reports, # Bot API-native HTML and PDF reports
    creator_admin,  # Bot API-native broadcasts and owner utilities
    admin,         # /help, channel command routing (registered last so it doesn't
                   # shadow the more specific per-feature MessageHandlers above)
)


def register(application: Application) -> None:
    """Register every handler module's commands/callbacks on `application`."""
    for module in _MODULES:
        module.register(application)
