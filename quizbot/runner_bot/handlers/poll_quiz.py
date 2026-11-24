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
from ..state import channel_poll_tasks, pollquiz_controls, pollquiz_sessions
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
    poll_open_period: Optional[int] = None, skip_control: Optional[dict] = None,
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
        sent = await safe_send_poll(
            ctx, chat_id, question=poll_q, options=poll_opts, type=Poll.QUIZ,
            explanation=poll_expl, is_anonymous=is_anonymous,
            message_thread_id=thread_id, **poll_kwargs,
        )
        if sent and score_session is not None:
            score_session.setdefault("polls", {})[sent.poll.id] = {
                "correct_option": list(correct_ids),
                "question_index": int(idx),
                "sent_time": time.time(),
                "message_id": sent.message_id,
            }
        if sent and skip_control is not None:
            skip_control["active_poll_id"] = sent.poll.id
            skip_control["active_poll_message_id"] = sent.message_id
        return bool(sent)
    except Exception as e:
        logger.error("_pollquiz_send_one idx=%d: %s", idx, e, exc_info=True)
        return False


async def run_channel_pollquiz(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, quiz: dict, is_anonymous: bool = True,
    thread_id: Optional[int] = None, auto_close_seconds: Optional[int] = None,
) -> None:
    """Publish forwardable quiz polls that stay open until /pollstop."""
    score_session: Optional[dict] = None
    control: Optional[dict] = None
    try:
        questions = quiz.get("questions", [])
        total = len(questions)
        do_shuffle = quiz.get("shuffle_options", False)
        shuffle_o_count = quiz.get("shuffle_options_count", 0)
        poll_delay = 3.5 if total <= 20 else 4.0 if total <= 50 else 5.0
        control = {
            "skip_event": None, "active_poll_id": None,
            "active_poll_message_id": None, "current_index": None,
            "skip_requested": False, "posting_complete": False,
            "message_thread_id": thread_id,
        }
        pollquiz_controls[chat_id] = control
        score_session = {
            "qid": str(quiz.get("qid") or quiz.get("question_set_id") or ""),
            "quiz": quiz, "quiz_name": quiz.get("quiz_name", "Quiz"),
            "questions": questions, "total_questions": total,
            "polls": {}, "participants": {}, "started_at": time.time(),
            "posting_complete": False, "finalizing": False,
            "finalized": False, "is_anonymous": bool(is_anonymous),
            "message_thread_id": thread_id,
        }
        pollquiz_sessions[chat_id] = score_session
        intro = (
            f"📣 <b>{quiz.get('quiz_name', 'Quiz')}</b>\n"
            f"❓ {total} questions • ♾️ Polls stay open until manually stopped."
        )
        if is_anonymous:
            intro += "\n\nThese polls are anonymous; individual scores are unavailable."
        else:
            intro += "\n\nParticipants can change their votes while polls remain open. Use /leaderboard for live standings."
        intro += "\nPolls can be forwarded. Use /skip while posting, or /pollstop to close all polls."
        await safe_send_message(ctx, chat_id, intro, parse_mode=ParseMode.HTML, thread_id=thread_id)
        await asyncio.sleep(1.0)
        for idx, q in enumerate(questions):
            if asyncio.current_task().cancelled():
                raise asyncio.CancelledError()
            skip_event = asyncio.Event()
            control.update({"skip_event": skip_event, "active_poll_id": None,
                "active_poll_message_id": None, "current_index": idx, "skip_requested": False})
            sent = await _pollquiz_send_one(
                ctx, chat_id, q, idx, total, do_shuffle,
                is_anonymous=is_anonymous, shuffle_count=shuffle_o_count,
                score_session=score_session, thread_id=thread_id,
                poll_open_period=None, skip_control=control,
            )
            try:
                await asyncio.wait_for(skip_event.wait(), timeout=poll_delay if sent else poll_delay + 2.0)
            except asyncio.TimeoutError:
                pass
            if control.get("skip_event") is skip_event:
                control.update({"skip_event": None, "active_poll_id": None,
                    "active_poll_message_id": None, "current_index": None, "skip_requested": False})
        control.update({"posting_complete": True, "skip_event": None,
            "active_poll_id": None, "active_poll_message_id": None,
            "current_index": None, "skip_requested": False})
        score_session["posting_complete"] = True
        await safe_send_message(ctx, chat_id,
            f"✅ All {total} polls are posted and remain open indefinitely. "
            "Use /leaderboard for live group standings, or /pollstop to close every poll and publish final results.",
            thread_id=thread_id)
    except asyncio.CancelledError:
        if score_session is None or not score_session.get("posting_complete"):
            await safe_send_message(ctx, chat_id,
                "🛑 Poll posting stopped. Use /pollstop to close already-posted polls.", thread_id=thread_id)
        raise
    except Exception as e:
        logger.error("run_channel_pollquiz error: %s", e, exc_info=True)
        await safe_send_message(ctx, chat_id,
            "⚠️ Poll quiz stopped because an error occurred. Already-posted polls remain open; use /pollstop to close them.",
            thread_id=thread_id)
    finally:
        current_task = asyncio.current_task()
        if channel_poll_tasks.get(chat_id) is current_task:
            channel_poll_tasks.pop(chat_id, None)
        if control is not None and pollquiz_controls.get(chat_id) is control:
            control["posting_complete"] = True
            control["skip_event"] = None


async def skip_active_pollquiz_question(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int,
) -> tuple[bool, str]:
    """Close the most recently posted poll and wake the /pollquiz poster."""
    control = pollquiz_controls.get(chat_id)
    task = channel_poll_tasks.get(chat_id)
    if not control or not task or task.done():
        return False, "⚠️ No poll quiz is currently posting questions."
    if control.get("posting_complete"):
        return False, "ℹ️ All questions have already been posted. Use /pollstop to end the poll quiz and close its remaining polls."

    event = control.get("skip_event")
    poll_id = control.get("active_poll_id")
    message_id = control.get("active_poll_message_id")
    question_index = control.get("current_index")
    if not isinstance(event, asyncio.Event) or not poll_id or not message_id or question_index is None:
        return False, "⚠️ There is no active poll to skip yet. Try again after the current poll appears."
    if control.get("skip_requested") or event.is_set():
        return False, "⏭️ A skip is already being processed."

    control["skip_requested"] = True
    try:
        await ctx.bot.stop_poll(chat_id=chat_id, message_id=message_id)
    except Exception as exc:
        logger.warning("Could not close /pollquiz poll for /skip in chat %s: %s", chat_id, exc)
        if pollquiz_controls.get(chat_id) is control and control.get("skip_event") is event:
            control["skip_requested"] = False
        return False, "⚠️ I couldn't close the current poll, so I haven't skipped it. Please try again."

    if (
        pollquiz_controls.get(chat_id) is control
        and control.get("skip_event") is event
        and control.get("current_index") == question_index
    ):
        event.set()
        return True, f"⏭️ Question {int(question_index) + 1} skipped. Moving to the next poll."
    return False, "ℹ️ That poll finished while /skip was being processed; no newer question was skipped."


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

    rows = [] if session.get("is_anonymous") else pollquiz_score_rows(session)
    if not session.get("is_anonymous"):
        await _send_pollquiz_scoreboard(ctx, chat_id, session, rows, thread_id=thread_id)
    else:
        await safe_send_message(ctx, chat_id,
            "🏁 All persistent anonymous polls have been closed. Individual scores are unavailable.",
            thread_id=thread_id)

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

        if not ctx.args or len(ctx.args) != 1:
            await safe_send_message(ctx, chat_id,
                "Usage: /pollquiz QUIZ_ID\nPolls remain open indefinitely. Use /pollstop to close them.")
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
            pollquiz_sessions.pop(chat_id, None)
            pollquiz_controls.pop(chat_id, None)
        elif not cancelled_task:
            await safe_send_message(ctx, chat_id, "⚠️ No persistent poll quiz is active in this chat.")
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
