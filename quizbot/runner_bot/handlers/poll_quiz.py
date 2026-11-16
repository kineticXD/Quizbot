"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Optional

from telegram import Poll, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from quizbot.database import AuthChatRepository, QuizRepository, get_db
from quizbot.shared import config
from quizbot.shared.rich_quiz import RichDispatchResult, enrich_question_dispatch
from quizbot.shared.utils import is_premium_user

from ..quiz_utils import shuffle_options_multi
from ..state import channel_poll_tasks, pollquiz_sessions
from ..telegram_utils import (
    prepare_poll_data,
    safe_send_message,
    safe_send_long_message,
    safe_send_poll,
    send_raw_api,
)

logger = logging.getLogger(__name__)

_ANON_ADMIN_ID = 1087968824
# Telegram Bot API permits poll auto-close durations from 5 seconds to 30 days.
MIN_POLL_OPEN_SECONDS = 5
MAX_POLL_OPEN_SECONDS = 2_628_000
MAX_POLLQUIZ_MINUTES = MAX_POLL_OPEN_SECONDS // 60


def _resolve_auto_close_seconds(
    value: Optional[int] = None, total: int = 1, poll_delay: float = 4.0,
) -> int:
    """Validate duration and reserve Telegram's close-period buffer for early polls."""
    seconds = config.POLLQUIZ_AUTO_CLOSE_SECONDS if value is None else int(value)
    if not MIN_POLL_OPEN_SECONDS <= seconds <= MAX_POLL_OPEN_SECONDS:
        raise ValueError(
            f"Poll quiz duration must be between {MIN_POLL_OPEN_SECONDS} seconds "
            f"and {MAX_POLL_OPEN_SECONDS} seconds (30 days)."
        )
    max_buffer = math.ceil(max(0, total - 1) * (poll_delay + 15.0) + 5.0)
    seconds = min(seconds, MAX_POLL_OPEN_SECONDS - max_buffer)
    if seconds < MIN_POLL_OPEN_SECONDS:
        raise ValueError("This quiz has too many questions for Telegram's maximum poll duration.")
    return seconds


def _poll_open_period(
    auto_close_seconds: int, total: int, poll_delay: float, question_index: int,
) -> int:
    """Align each question's expiry to the last poll's auto-close deadline."""
    remaining = max(0, total - question_index - 1)
    posting_buffer = math.ceil(remaining * (poll_delay + 15.0) + 5.0)
    return min(MAX_POLL_OPEN_SECONDS, auto_close_seconds + posting_buffer)


def _format_duration(seconds: int) -> str:
    minutes, remainder = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    if minutes:
        parts.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
    if remainder and not hours:
        parts.append(f"{remainder} second" + ("s" if remainder != 1 else ""))
    return " ".join(parts) or "less than a minute"


async def _pollquiz_send_one(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, q: dict, idx: int, total: int,
    do_shuffle: bool, is_anonymous: bool = True, shuffle_count: int = 0,
    score_session: Optional[dict] = None, thread_id: Optional[int] = None,
    poll_open_period: Optional[int] = None,
) -> bool:
    """Send one question as an auto-closing poll. Returns True on success.
    Applies the same rich-text pre-pass as the timed group/private senders:
    a rich question/options/reply_text is pre-sent via sendRichMessage
    before the poll goes out (see `quizbot.shared.rich_quiz`)."""
    try:
        options = list(q["options"])
        correct_id = q["correct_option_id"]
        correct_ids = correct_id if isinstance(correct_id, list) else [correct_id]
        is_multi = len(correct_ids) > 1
        reply_text = q.get("reply_text")
        file_id = q.get("file_id")

        if do_shuffle:
            options, correct_ids = shuffle_options_multi(options, correct_ids, shuffle_count)

        rich_res: RichDispatchResult = await enrich_question_dispatch(
            lambda method, params: send_raw_api(ctx, method, params),
            lambda text: safe_send_message(ctx, chat_id, text, parse_mode=ParseMode.HTML, thread_id=thread_id),
            chat_id, q, idx, total, thread_id=thread_id,
        )
        if rich_res.rich_sent:
            await asyncio.sleep(1)

        _rt = None if rich_res.suppress_reply_text else reply_text

        if file_id:
            try:
                await ctx.bot.send_photo(chat_id=chat_id, photo=file_id, message_thread_id=thread_id)
                await asyncio.sleep(0.5)
            except Exception:
                pass

        _q_text = rich_res.poll_question_override or q["question"]
        poll_q, poll_opts, poll_expl, overflow, poll_desc = prepare_poll_data(
            _q_text, options, correct_ids[0], q.get("explanation"), _rt, idx, total
        )
        if rich_res.poll_options_override:
            poll_opts = rich_res.poll_options_override
        if rich_res.suppress_description:
            poll_desc = None
            overflow = None

        if overflow:
            await safe_send_long_message(ctx, chat_id, overflow, thread_id=thread_id)
            await asyncio.sleep(0.5)

        poll_kwargs: dict[str, Any] = {}
        if is_multi:
            poll_kwargs["correct_option_ids"] = correct_ids
            poll_kwargs["allows_multiple_answers"] = True
        else:
            poll_kwargs["correct_option_id"] = correct_ids[0]
        if poll_desc:
            poll_kwargs["description"] = poll_desc
        if poll_open_period is not None:
            poll_kwargs["open_period"] = int(poll_open_period)

        sent = await safe_send_poll(
            ctx, chat_id, question=poll_q, options=poll_opts, type=Poll.QUIZ,
            explanation=poll_expl, is_anonymous=is_anonymous,
            message_thread_id=thread_id, **poll_kwargs,
        )
        if sent and score_session is not None and not is_anonymous:
            score_session["polls"][sent.poll.id] = {
                "correct_option": list(correct_ids),
                "question_index": int(idx),
                "sent_time": time.time(),
                "message_id": sent.message_id,
            }
        return bool(sent)
    except Exception as e:
        logger.error("_pollquiz_send_one idx=%d: %s", idx, e, exc_info=True)
        return False


async def run_channel_pollquiz(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, quiz: dict, is_anonymous: bool = True,
    thread_id: Optional[int] = None, auto_close_seconds: Optional[int] = None,
) -> None:
    """Post questions as auto-closing polls and automatically publish group rankings.

    Telegram never exposes voter identities for anonymous channel polls, so
    channel mode deliberately has no individual leaderboard.
    """
    score_session: Optional[dict] = None
    try:
        questions = quiz.get("questions", [])
        total = len(questions)
        do_shuffle = quiz.get("shuffle_options", False)
        shuffle_o_count = quiz.get("shuffle_options_count", 0)
        poll_delay = 3.5 if total <= 20 else 4.0 if total <= 50 else 5.0
        auto_close_seconds = _resolve_auto_close_seconds(auto_close_seconds, total, poll_delay)

        if not is_anonymous:
            score_session = {
                "qid": str(quiz.get("qid") or quiz.get("question_set_id") or ""),
                "quiz": quiz,
                "quiz_name": quiz.get("quiz_name", "Quiz"),
                "questions": questions,
                "total_questions": total,
                "polls": {},
                "participants": {},
                "started_at": time.time(),
                "auto_close_seconds": auto_close_seconds,
                "posting_complete": False,
                "finalizing": False,
                "finalized": False,
                "message_thread_id": thread_id,
            }
            pollquiz_sessions[chat_id] = score_session
        else:
            # Remove stale group scoring state if this chat is now running an
            # anonymous channel-style poll series.
            pollquiz_sessions.pop(chat_id, None)

        await safe_send_message(
            ctx, chat_id,
            f"📣 <b>{quiz.get('quiz_name', 'Quiz')}</b>\n"
            f"❓ {total} questions • ⏳ Polls close automatically after {_format_duration(auto_close_seconds)}."
            + ("\n\nThe final group leaderboard will be posted automatically when the quiz ends. /leaderboard shows a live snapshot; /pollstop remains available for an early manual finish."
               if score_session is not None else "\n\nThese channel polls are anonymous, so Telegram does not provide individual scores or a personal leaderboard."),
            parse_mode=ParseMode.HTML, thread_id=thread_id,
        )
        await asyncio.sleep(1.5)

        for idx, q in enumerate(questions):
            if asyncio.current_task().cancelled():
                raise asyncio.CancelledError()
            sent = await _pollquiz_send_one(
                ctx, chat_id, q, idx, total, do_shuffle,
                is_anonymous=is_anonymous, shuffle_count=shuffle_o_count,
                score_session=score_session, thread_id=thread_id,
                poll_open_period=_poll_open_period(auto_close_seconds, total, poll_delay, idx),
            )
            await asyncio.sleep(poll_delay if sent else poll_delay + 2.0)

        if score_session is not None:
            score_session["posting_complete"] = True
            await safe_send_message(
                ctx, chat_id,
                f"✅ All {total} questions are posted. The polls will close automatically in "
                f"{_format_duration(auto_close_seconds)}; the final leaderboard will then appear here.",
                thread_id=thread_id,
            )
            # Each Telegram poll has an open_period. The extra posting buffer
            # ensures even the first poll remains open until this final window
            # (measured from the last question) has elapsed.
            await asyncio.sleep(auto_close_seconds + 7)
            if pollquiz_sessions.get(chat_id) is score_session and not score_session.get("finalized"):
                logger.info("Auto-finalizing poll quiz for chat=%s", chat_id)
                await finalize_pollquiz(ctx, chat_id)
        else:
            # Channel polls are anonymous by Telegram design. They still close
            # automatically, but no individual ranking can be calculated.
            await safe_send_message(
                ctx, chat_id,
                f"✅ All {total} anonymous channel polls are posted. They will close automatically in "
                f"{_format_duration(auto_close_seconds)}.",
                thread_id=thread_id,
            )
            await asyncio.sleep(auto_close_seconds + 7)
            await safe_send_message(
                ctx, chat_id,
                "🏁 <b>Quiz ended.</b> All poll questions are now closed. "
                "Individual leaderboard scores are unavailable for anonymous channel polls.",
                parse_mode=ParseMode.HTML, thread_id=thread_id,
            )
    except asyncio.CancelledError:
        if score_session is None or not score_session.get("posting_complete"):
            await safe_send_message(ctx, chat_id, "🛑 Poll quiz posting stopped.", thread_id=thread_id)
        raise
    except Exception as e:
        logger.error("run_channel_pollquiz error: %s", e, exc_info=True)
        await safe_send_message(ctx, chat_id, "⚠️ Poll quiz stopped because an error occurred. Check the bot logs.", thread_id=thread_id)
    finally:
        channel_poll_tasks.pop(chat_id, None)


def pollquiz_score_rows(session: dict) -> list[dict]:
    """Calculate the live scores from non-anonymous poll answers."""
    quiz = session.get("quiz", {})
    correct_mark = float(quiz.get("correct_mark", quiz.get("correct_marks", 1)) or 1)
    negative_marking = float(quiz.get("negative_marking", quiz.get("negative_marks", 0)) or 0)
    rows: list[dict] = []
    for uid, participant in session.get("participants", {}).items():
        correct = wrong = 0
        answers = participant.get("answers", {})
        last_answer_time = float(session.get("started_at", 0))
        for poll_id, answer in answers.items():
            poll = session.get("polls", {}).get(poll_id)
            if not poll:
                continue
            last_answer_time = max(last_answer_time, float(answer.get("time", last_answer_time)))
            from ..quiz_utils import is_correct
            if is_correct(answer.get("option", []), poll.get("correct_option", [])):
                correct += 1
            else:
                wrong += 1
        rows.append({
            "user_id": int(uid),
            "name": participant.get("name") or "Aspirant",
            "correct": correct,
            "wrong": wrong,
            "score": correct * correct_mark - wrong * negative_marking,
            "total_time": max(0.0, last_answer_time - float(session.get("started_at", last_answer_time))),
            "answered": len(answers),
            "answers": answers,
        })
    rows.sort(key=lambda item: (item["score"], item["correct"], -item["total_time"]), reverse=True)
    return rows


async def handle_pollquiz_poll_answer(
    poll_id: str, user_id: int, option_ids: list[int], current_time: float, user_name: str,
) -> bool:
    """Attach Telegram poll_answer updates to the matching /pollquiz session."""
    for session in list(pollquiz_sessions.values()):
        poll = session.get("polls", {}).get(poll_id)
        if not poll or session.get("finalized"):
            continue
        participants = session.setdefault("participants", {})
        participant = participants.setdefault(
            int(user_id), {"name": user_name or "Aspirant", "answers": {}}
        )
        if user_name:
            participant["name"] = user_name
        answers = participant.setdefault("answers", {})
        if option_ids:
            answers[poll_id] = {"option": list(option_ids), "time": float(current_time)}
        else:
            answers.pop(poll_id, None)
            if not answers:
                participants.pop(int(user_id), None)
        return True
    return False


async def finalize_pollquiz(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    """Close remaining polls and persist a participant leaderboard snapshot."""
    session = pollquiz_sessions.get(chat_id)
    if not session:
        return False
    thread_id = session.get("message_thread_id")
    if session.get("finalized"):
        return True
    if session.get("finalizing"):
        return False
    session["finalizing"] = True

    for poll in session.get("polls", {}).values():
        message_id = poll.get("message_id")
        if not message_id:
            continue
        try:
            await ctx.bot.stop_poll(chat_id=chat_id, message_id=message_id)
        except Exception as exc:
            logger.debug("Could not close poll %s in %s: %s", message_id, chat_id, exc)

    rows = pollquiz_score_rows(session)
    await _send_pollquiz_scoreboard(ctx, chat_id, session, rows, thread_id=thread_id)

    persisted = True
    if rows:
        quiz_data = dict(session.get("quiz", {}))
        quiz_data["question_set_id"] = session.get("qid")
        quiz_data["polls"] = session.get("polls", {})
        try:
            from .quiz_play import _record_attempt_and_report
            persisted = await _record_attempt_and_report(
                ctx, chat_id, quiz_data, rows,
                chat_title="Poll Quiz", protect_type=False, thread_id=thread_id,
            )
        except Exception:
            logger.exception("Unable to persist /pollquiz leaderboard for chat=%s", chat_id)
            persisted = False
    session["finalized"] = bool(persisted)
    session["finalizing"] = False
    return bool(persisted)


async def _send_pollquiz_scoreboard(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, session: dict, rows: list[dict],
    thread_id: Optional[int] = None,
) -> None:
    """Publish the poll-quiz final leaderboard using the shared rich table."""
    # Lazy import avoids a module cycle: quiz_play routes incoming poll answers
    # to poll_quiz, while poll_quiz reuses the central leaderboard renderer.
    from .quiz_play import _send_leaderboard_table

    total = int(session.get("total_questions", 0) or 0)
    title = str(session.get("quiz_name") or "Poll Quiz")
    await _send_leaderboard_table(
        ctx,
        chat_id,
        title,
        rows,
        total_questions=total,
        status="Final Poll Results",
        subtitle=f"Poll quiz completed · {total} questions · {len(rows)} participants",
        thread_id=thread_id,
        limit=20,
    )


async def pollquiz_channel_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/pollquiz QUIZID [minutes]` -- auto-closes polls and publishes group scores."""
    chat_id = update.message.chat_id
    try:
        chat_type = update.message.chat.type
        user_id = update.message.from_user.id if update.message.from_user else None

        try:
            await ctx.bot.delete_message(chat_id, update.message.message_id)
        except Exception:
            pass

        if not ctx.args or len(ctx.args) > 2:
            await safe_send_message(
                ctx, chat_id,
                "Usage: /pollquiz QUIZ_ID [minutes]\nDefault duration: 60 minutes. "
                "The final leaderboard is posted automatically when polls close.",
            )
            return

        try:
            if len(ctx.args) == 2:
                duration_minutes = int(ctx.args[1])
                if not 1 <= duration_minutes <= MAX_POLLQUIZ_MINUTES:
                    raise ValueError
                duration_seconds = duration_minutes * 60
            else:
                duration_seconds = _resolve_auto_close_seconds()
        except (TypeError, ValueError):
            await safe_send_message(
                ctx, chat_id,
                f"Duration must be 1–{MAX_POLLQUIZ_MINUTES} minutes. "
                "Example: /pollquiz QUIZ_ID 60",
            )
            return

        if user_id and user_id != _ANON_ADMIN_ID and not await is_premium_user(user_id):
            await safe_send_message(ctx, chat_id, "Please help us to make this project more valuable by purchasing premium! Thanks")
            return

        existing = channel_poll_tasks.get(chat_id)
        if existing and not existing.done():
            await safe_send_message(ctx, chat_id, "⚠️ A poll quiz is already running. Use /pollstop to stop it.")
            return
        previous = pollquiz_sessions.get(chat_id)
        if previous and not previous.get("finalized"):
            await safe_send_message(ctx, chat_id, "⚠️ A previous poll quiz still has unfinalized answers. Run /pollstop first to save its leaderboard.")
            return

        qid = ctx.args[0]
        quiz_repo = QuizRepository(get_db())
        quiz = await quiz_repo.get(qid)
        if not quiz:
            await safe_send_message(ctx, chat_id, "❌ Invalid QuestionSetID.")
            return

        questions = quiz.get("questions", [])
        if not questions:
            await safe_send_message(ctx, chat_id, "❌ Quiz has no questions.")
            return

        is_anon = chat_type == "channel"
        thread_id = getattr(update.effective_message, "message_thread_id", None)
        task = asyncio.create_task(
            run_channel_pollquiz(
                ctx, chat_id, quiz, is_anonymous=is_anon, thread_id=thread_id,
                auto_close_seconds=duration_seconds,
            )
        )
        channel_poll_tasks[chat_id] = task
    except Exception as e:
        logger.error("pollquiz_channel_command error: %s", e, exc_info=True)


async def pollstop_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/pollstop` -- cancels a running `/pollquiz` in this chat."""
    chat_id = update.message.chat_id
    try:
        try:
            await ctx.bot.delete_message(chat_id, update.message.message_id)
        except Exception:
            pass

        task = channel_poll_tasks.get(chat_id)
        cancelled_task = bool(task and not task.done())
        if cancelled_task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if chat_id in pollquiz_sessions:
            await finalize_pollquiz(ctx, chat_id)
        elif not cancelled_task:
            await safe_send_message(ctx, chat_id, "⚠️ No poll quiz is running.")
    except Exception as e:
        logger.error("pollstop_command error: %s", e, exc_info=True)


async def check_channel_paid_access(quiz: dict, chat_id: int) -> Optional[str]:
    """For a paid quiz posted from a channel context, return an error
    message if the channel isn't authorised, or None if allowed."""
    creator_id = quiz.get("creator_id")
    if quiz.get("quiz_type") != "paid" or not creator_id:
        return None
    auth_repo = AuthChatRepository(get_db())
    auth_users = await auth_repo.get(creator_id)
    if chat_id in auth_users:
        return None
    return f"❌ Paid quiz. Contact creator ID {creator_id} for access."


def register(application: Application) -> None:
    application.add_handler(CommandHandler("pollquiz", pollquiz_channel_command))
    application.add_handler(CommandHandler("pollstop", pollstop_command))
