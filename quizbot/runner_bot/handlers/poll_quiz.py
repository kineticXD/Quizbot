"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Optional

from telegram import Poll, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from quizbot.database import AuthChatRepository, PollQuizSettingsRepository, QuizRepository, get_db
from quizbot.shared.rich_quiz import RichDispatchResult, enrich_question_dispatch
from quizbot.shared.poll_target import resolve_poll_destination
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


def _normalise_caselet_question(item: dict) -> dict:
    """Detach a shared caselet from a poll question.

    New quiz records store the passage in ``context``. Older records may have
    the passage copied into ``question`` by the historic parser; detect and
    split that prefix here so already-saved quizzes also publish cleanly.
    """
    q = dict(item)
    question = str(q.get("question") or "").strip()
    context = str(q.get("context") or q.get("caselet") or "").strip()

    if not context and question:
        try:
            from quizbot.creator_bot.parsing import _extract_shared_passage
            context = _extract_shared_passage(question).strip()
        except Exception:
            context = ""

    if context and question.startswith(context):
        question = question[len(context):].lstrip("\n ")

    # Older source imports stored their shared passage in reply_text. Only
    # promote it when it is demonstrably the same prefix embedded in question;
    # short per-question notes/explanations remain attached to their poll.
    reply_text = str(q.get("reply_text") or "").strip()
    if context and reply_text and reply_text == context:
        q["reply_text"] = None

    q["question"] = question or str(q.get("question") or "").strip()
    q["context"] = context or None
    return q


def _normalise_caselet_questions(items: list[dict]) -> list[dict]:
    """Normalise a quiz and upgrade legacy repeated reply_text caselets.

    Some source-generated quiz records used reply_text for context. When the
    same substantial text is repeated on adjacent questions, treat it as one
    shared caselet and display it once for that contiguous group.
    """
    questions = [_normalise_caselet_question(item) for item in items]
    for index, item in enumerate(questions):
        if item.get("context") or item.get("reply_text") is None:
            continue
        candidate = str(item.get("reply_text") or "").strip()
        if len(candidate) < 80:
            continue
        previous_same = index > 0 and str(questions[index - 1].get("reply_text") or "").strip() == candidate
        next_same = index + 1 < len(questions) and str(questions[index + 1].get("reply_text") or "").strip() == candidate
        if previous_same or next_same:
            item["context"] = candidate
            item["reply_text"] = None
    return questions


async def _pollquiz_send_one(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, q: dict, idx: int, total: int,
    do_shuffle: bool, is_anonymous: bool = True, shuffle_count: int = 0,
    score_session: Optional[dict] = None, thread_id: Optional[int] = None,
    skip_control: Optional[dict] = None,
) -> bool:
    """Send one forwardable Telegram quiz poll with no automatic close time.
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
            poll_record = {
                "question_index": int(idx),
                "sent_time": time.time(),
                "message_id": sent.message_id,
            }
            if score_session.get("collect_scores"):
                poll_record["correct_option"] = list(correct_ids)
            # Track every sent poll, including anonymous polls, so /pollclose
            # or /pollfinish can manually close them later.
            score_session.setdefault("polls", {})[sent.poll.id] = poll_record
        if sent and skip_control is not None:
            skip_control["active_poll_id"] = sent.poll.id
            skip_control["active_poll_message_id"] = sent.message_id
        return bool(sent)
    except Exception as e:
        logger.error("_pollquiz_send_one idx=%d: %s", idx, e, exc_info=True)
        return False


async def run_channel_pollquiz(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, quiz: dict, is_anonymous: bool = True,
    thread_id: Optional[int] = None,
    shuffle_questions: Optional[bool] = None, shuffle_options: Optional[bool] = None,
    publish_scoreboard: bool = True,
) -> None:
    """Post forwardable polls that stay open until explicitly stopped.

    No automatic expiry is sent to Telegram. Polls remain active until an
    administrator manually closes them; Telegram channel polls are anonymous.
    """
    score_session: Optional[dict] = None
    control: Optional[dict] = None
    try:
        questions = _normalise_caselet_questions([dict(item) for item in quiz.get("questions", [])])
        total = len(questions)
        do_shuffle = bool(quiz.get("shuffle_options", False) if shuffle_options is None else shuffle_options)
        do_shuffle_questions = bool(quiz.get("shuffle_questions", False) if shuffle_questions is None else shuffle_questions)
        shuffle_o_count = quiz.get("shuffle_options_count", 0)
        if do_shuffle_questions and len(questions) > 1:
            # Keep questions that share a passage/reference together. The
            # order of passage groups remains stable; only questions within
            # each contiguous group are shuffled.
            groups: list[tuple[str, list[dict]]] = []
            for item in questions:
                context_text = str(item.get("context") or "").strip()
                if groups and groups[-1][0] == context_text:
                    groups[-1][1].append(item)
                else:
                    groups.append((context_text, [item]))
            random.shuffle(groups)
            questions = []
            for _context, group in groups:
                random.shuffle(group)
                questions.extend(group)
        # This delay only spaces out message delivery to avoid flooding Telegram.
        # It does not affect the lifetime of any published poll.
        poll_delay = 3.5 if total <= 20 else 4.0 if total <= 50 else 5.0

        control = {
            "skip_event": None,
            "active_poll_id": None,
            "active_poll_message_id": None,
            "current_index": None,
            "skip_requested": False,
            "posting_complete": False,
            "message_thread_id": thread_id,
        }
        pollquiz_controls[chat_id] = control

        collect_scores = bool(not is_anonymous and publish_scoreboard)
        # Keep a session for every run, even anonymous ones, because it stores
        # the poll message IDs needed by /pollclose and /pollfinish.
        score_session = {
            "qid": str(quiz.get("qid") or quiz.get("question_set_id") or ""),
            "quiz": quiz,
            "quiz_name": quiz.get("quiz_name", "Quiz"),
            "questions": questions,
            "total_questions": total,
            "polls": {},
            "participants": {},
            "started_at": time.time(),
            "collect_scores": collect_scores,
            "is_anonymous": bool(is_anonymous),
            "posting_complete": False,
            "finalizing": False,
            "finalized": False,
            "message_thread_id": thread_id,
        }
        pollquiz_sessions[chat_id] = score_session

        await safe_send_message(
            ctx, chat_id,
            f"📣 <b>{quiz.get('quiz_name', 'Quiz')}</b>\n"
            f"❓ {total} questions • ♾️ Polls have no expiry timer."
            + ("\n\nThe polls are forwardable. /leaderboard shows live standings; /pollstop stops publishing, /pollclose closes polls, and /pollfinish posts final results."
               if collect_scores else ("\n\nThese polls are anonymous, so Telegram does not provide individual scores. Use /pollclose when you want to close them." if is_anonymous else "\n\nPolls are public, but individual score collection is disabled in your settings. Use /pollclose when you want to close them.")),
            parse_mode=ParseMode.HTML, thread_id=thread_id,
        )
        await asyncio.sleep(1.5)

        last_caselet_context = None
        published_count = 0
        failed_index: Optional[int] = None
        for idx, q in enumerate(questions):
            if asyncio.current_task().cancelled():
                raise asyncio.CancelledError()

            caselet_context = str(q.get("context") or "").strip()
            if caselet_context and caselet_context != last_caselet_context:
                await safe_send_long_message(
                    ctx, chat_id, "📖 CASELET / SHARED PASSAGE\n\n" + caselet_context,
                    thread_id=thread_id,
                )
                await asyncio.sleep(0.8)
            last_caselet_context = caselet_context or None

            skip_event = asyncio.Event()
            control.update({
                "skip_event": skip_event,
                "active_poll_id": None,
                "active_poll_message_id": None,
                "current_index": idx,
                "skip_requested": False,
            })
            sent = await _pollquiz_send_one(
                ctx, chat_id, q, idx, total, do_shuffle,
                is_anonymous=is_anonymous, shuffle_count=shuffle_o_count,
                score_session=score_session, thread_id=thread_id,
                skip_control=control,
            )
            if sent:
                published_count += 1
            else:
                failed_index = idx
                await safe_send_message(
                    ctx, chat_id,
                    "⚠️ Shikimori could not publish this poll. Check that the bot is allowed to send polls in this group/topic (Chat Permissions → Send Polls). The bot does not need administrator status when normal members are allowed to send polls.",
                    thread_id=thread_id,
                )
                break
            delay = poll_delay
            try:
                await asyncio.wait_for(skip_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            if control.get("skip_event") is skip_event:
                control.update({
                    "skip_event": None,
                    "active_poll_id": None,
                    "active_poll_message_id": None,
                    "current_index": None,
                    "skip_requested": False,
                })

        control.update({
            "posting_complete": True,
            "skip_event": None,
            "active_poll_id": None,
            "active_poll_message_id": None,
            "current_index": None,
            "skip_requested": False,
        })
        score_session["posting_complete"] = True
        if published_count < total:
            posted_message = (
                f"⚠️ Published {published_count} of {total} polls. Publishing stopped at question "
                f"{(failed_index + 1) if failed_index is not None else published_count + 1}. "
                "Check the bot's Send Messages and Send Polls permissions in the selected topic."
            )
        elif collect_scores:
            posted_message = (
                f"✅ All {total} questions are posted. Every poll remains open indefinitely. "
                "Use /leaderboard for live standings. /pollstop stops further publishing; /pollclose closes polls; /pollfinish closes polls and posts final results."
            )
        else:
            posted_message = (
                f"✅ All {total} {'anonymous' if is_anonymous else 'public'} polls are posted. "
                "They remain open indefinitely. /pollstop stops further publishing; /pollclose closes them."
            )
            if is_anonymous:
                posted_message += " Individual scores are unavailable for anonymous polls."
            else:
                posted_message += " Individual score collection is disabled for this run."
        await safe_send_message(ctx, chat_id, posted_message, thread_id=thread_id)
    except asyncio.CancelledError:
        if score_session is None or not score_session.get("posting_complete"):
            await safe_send_message(ctx, chat_id, "🛑 Poll quiz posting stopped.", thread_id=thread_id)
        raise
    except Exception as e:
        logger.error("run_channel_pollquiz error: %s", e, exc_info=True)
        await safe_send_message(ctx, chat_id, "⚠️ Poll quiz stopped because an error occurred. Check the bot logs.", thread_id=thread_id)
    finally:
        # Do not let an older finishing task remove a newer task registered
        # for the same chat after a quick stop/restart cycle.
        current_task = asyncio.current_task()
        if channel_poll_tasks.get(chat_id) is current_task:
            channel_poll_tasks.pop(chat_id, None)
        if control is not None and pollquiz_controls.get(chat_id) is control:
            pollquiz_controls.pop(chat_id, None)


async def skip_active_pollquiz_question(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int,
) -> tuple[bool, str]:
    """Close the most recently posted poll and wake the /pollquiz poster."""
    control = pollquiz_controls.get(chat_id)
    task = channel_poll_tasks.get(chat_id)
    if not control or not task or task.done():
        return False, "⚠️ No poll quiz is currently posting questions."
    if control.get("posting_complete"):
        return False, "ℹ️ All questions have already been posted. Use /pollclose to close polls, or /pollfinish to close them and publish final results."

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

    rows = pollquiz_score_rows(session) if session.get("collect_scores") else []
    if session.get("collect_scores"):
        await _send_pollquiz_scoreboard(ctx, chat_id, session, rows, thread_id=thread_id)
    else:
        note = (
            "🏁 <b>Poll quiz closed.</b> All tracked polls have been stopped. "
            "Individual scores are unavailable for anonymous polls."
            if session.get("is_anonymous") else
            "🏁 <b>Poll quiz closed.</b> All tracked polls have been stopped. Individual score collection was disabled."
        )
        await safe_send_message(ctx, chat_id, note, parse_mode=ParseMode.HTML, thread_id=thread_id)

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


async def polltarget_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Set/show/clear the invoking user's default forum topic destination.

    With no argument, call this command from inside the target forum topic.
    A numeric thread ID may also be supplied while inside the target group.
    """
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    repo = PollQuizSettingsRepository(get_db())
    args = list(ctx.args or [])
    if args and args[0].lower() == "show":
        settings = await repo.get(user.id)
        target_chat = settings.get("poll_target_chat_id")
        target_thread = settings.get("poll_target_thread_id")
        if target_chat is None or target_thread is None:
            text = (
                "📌 <b>No poll topic is saved.</b>\n\n"
                "Open the forum topic where you want Shikimori to publish, then send "
                "<code>/polltarget</code> there."
            )
        else:
            text = (
                "📌 <b>Your saved poll destination</b>\n\n"
                f"Group ID: <code>{int(target_chat)}</code>\n"
                f"Topic ID: <code>{int(target_thread)}</code>\n\n"
                "Use <code>/pollquiz QUIZ_ID</code> in this group or in your private chat with Shikimori."
            )
        await message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    if args and args[0].lower() == "clear":
        await repo.clear_topic_target(user.id)
        await message.reply_text("✅ Your saved poll destination has been cleared.")
        return

    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text(
            "Open the destination supergroup and run <code>/polltarget</code> "
            "inside the topic you want to use. You can also pass a topic ID: "
            "<code>/polltarget 123456</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not bool(getattr(message.chat, "is_forum", False)):
        await message.reply_text("This group does not have forum topics enabled.")
        return

    thread_id: Optional[int] = None
    if args:
        if len(args) != 1:
            await message.reply_text("Usage: /polltarget [topic_id|show|clear]")
            return
        try:
            thread_id = int(args[0])
        except ValueError:
            await message.reply_text("Topic ID must be a positive integer. Or run /polltarget inside the target topic.")
            return
    else:
        thread_id = getattr(message, "message_thread_id", None)

    if thread_id is None or int(thread_id) <= 0:
        await message.reply_text(
            "I couldn't detect a forum topic from this message. Open the exact topic first, "
            "then send <code>/polltarget</code>. Alternatively use <code>/polltarget TOPIC_ID</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    await repo.set_topic_target(user.id, message.chat_id, int(thread_id))
    await safe_send_message(
        ctx, message.chat_id,
        "📌 <b>Poll destination saved.</b>\n\n"
        "Shikimori will publish the caselet passages and every poll in this topic. "
        "You can now use <code>/pollquiz QUIZ_ID</code> from this group or from your private chat.",
        parse_mode=ParseMode.HTML,
        thread_id=int(thread_id),
    )


async def pollquiz_channel_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """`/pollquiz QUIZ_ID [anonymous|public]` -- publish permanent forwardable polls."""
    message = update.effective_message
    if message is None:
        return
    source_chat_id = message.chat_id
    source_chat_type = message.chat.type
    try:
        user_id = update.effective_user.id if update.effective_user else None

        try:
            await ctx.bot.delete_message(source_chat_id, message.message_id)
        except Exception:
            # Bots do not need delete-message admin rights to publish polls.
            pass

        if not ctx.args or len(ctx.args) > 2:
            await safe_send_message(
                ctx, source_chat_id,
                "Usage: /pollquiz QUIZ_ID [anonymous|public]\n"
                "Polls have no expiry timer. /pollstop stops publishing; /pollclose or /pollfinish closes published polls. "
                "Use /pollsettings to configure anonymity and shuffling.",
            )
            return

        settings_repo = PollQuizSettingsRepository(get_db()) if user_id else None
        settings = (
            await settings_repo.get(user_id)
            if settings_repo else {
                "anonymous": True, "shuffle_questions": False,
                "shuffle_options": False, "scoreboard": False,
                "poll_target_chat_id": None, "poll_target_thread_id": None,
            }
        )
        current_thread_id = getattr(message, "message_thread_id", None)
        chat_id, thread_id, target_source = resolve_poll_destination(
            source_chat_id, source_chat_type, current_thread_id, settings,
        )
        if current_thread_id is not None and settings_repo is not None:
            # Using /pollquiz from a topic automatically makes it the user's
            # preferred destination for later runs started from General/DM.
            await settings_repo.set_topic_target(user_id, source_chat_id, int(current_thread_id))
        is_channel = source_chat_type == ChatType.CHANNEL
        is_anon = True if is_channel else bool(settings.get("anonymous", False))
        if len(ctx.args) == 2:
            requested_mode = str(ctx.args[1]).strip().lower()
            if requested_mode in ("anonymous", "anon"):
                is_anon = True
            elif requested_mode in ("public", "scored"):
                if is_channel:
                    await safe_send_message(ctx, source_chat_id, "Channel polls are anonymous by Telegram design. Run this in a group for public polls and scores.")
                    return
                is_anon = False
            else:
                await safe_send_message(
                    ctx, source_chat_id,
                    "Timer arguments have been removed. Use /pollquiz QUIZ_ID or /pollquiz QUIZ_ID public.",
                )
                return

        if user_id and user_id != _ANON_ADMIN_ID and not await is_premium_user(user_id):
            await safe_send_message(ctx, source_chat_id, "Please help us to make this project more valuable by purchasing premium! Thanks")
            return

        existing = channel_poll_tasks.get(chat_id)
        if existing and not existing.done():
            await safe_send_message(ctx, source_chat_id, "⚠️ A poll quiz is already being published. Use /pollstop to stop sending new polls.")
            return
        previous = pollquiz_sessions.get(chat_id)
        if previous and not previous.get("finalized") and not previous.get("closed"):
            await safe_send_message(ctx, source_chat_id, "⚠️ Previous polls are still open. Use /pollclose to close them without a leaderboard, or /pollfinish to close them and publish final results.")
            return

        qid = ctx.args[0]
        quiz_repo = QuizRepository(get_db())
        quiz = await quiz_repo.get(qid)
        if not quiz:
            await safe_send_message(ctx, source_chat_id, "❌ Invalid QuestionSetID.")
            return

        questions = quiz.get("questions", [])
        if not questions:
            await safe_send_message(ctx, source_chat_id, "❌ Quiz has no questions.")
            return

        if (
            bool(getattr(message.chat, "is_forum", False))
            and source_chat_type in (ChatType.GROUP, ChatType.SUPERGROUP)
            and current_thread_id is None
            and target_source == "current_chat"
        ):
            await safe_send_message(
                ctx, source_chat_id,
                "📌 This group has forum topics, but no destination topic is selected.\n\n"
                "Open the topic where you want the polls and send <code>/polltarget</code> there. "
                "Then use <code>/pollquiz QUIZ_ID</code> from General or from your private chat with Shikimori.",
                parse_mode=ParseMode.HTML,
            )
            return

        if chat_id != source_chat_id:
            await safe_send_message(
                ctx, source_chat_id,
                f"📣 Publishing <b>{quiz.get('quiz_name', 'Quiz')}</b> in your saved forum topic.",
                parse_mode=ParseMode.HTML,
            )

        # Preflight the actual destination. A bot does not need administrator
        # status just to send messages/polls, but it must be a group member
        # with permission to send messages in the selected topic.
        destination_check = await safe_send_message(
            ctx, chat_id,
            "📌 Shikimori poll destination confirmed. Preparing your quiz…",
            thread_id=thread_id,
        )
        if destination_check is None:
            await safe_send_message(
                ctx, source_chat_id,
                "⚠️ I couldn't post in the selected group topic. Make sure Shikimori is a member of that group and has permission to send messages there. Administrator rights are not required for ordinary poll publishing.",
            )
            return

        task = asyncio.create_task(
            run_channel_pollquiz(
                ctx, chat_id, quiz, is_anonymous=is_anon, thread_id=thread_id,
                shuffle_questions=bool(settings.get("shuffle_questions", False)),
                shuffle_options=bool(settings.get("shuffle_options", False)),
                publish_scoreboard=bool(settings.get("scoreboard", True)),
            )
        )
        channel_poll_tasks[chat_id] = task
    except Exception as e:
        logger.error("pollquiz_channel_command error: %s", e, exc_info=True)


async def _resolve_poll_control_destination(update: Update) -> tuple[int, Optional[int]]:
    """Resolve control commands to the same group/topic used by /pollquiz.

    This lets an owner stop/close/finalize from the configured group, its
    General chat, or a private chat with Shikimori.
    """
    message = update.effective_message
    if message is None:
        return 0, None
    user = update.effective_user
    current_thread = getattr(message, "message_thread_id", None)
    active_session = pollquiz_sessions.get(int(message.chat_id))
    if active_session and active_session.get("message_thread_id") is not None:
        return int(message.chat_id), int(active_session["message_thread_id"])
    if current_thread is not None:
        return int(message.chat_id), int(current_thread)
    if user:
        settings = await PollQuizSettingsRepository(get_db()).get(user.id)
        target_chat = settings.get("poll_target_chat_id")
        target_thread = settings.get("poll_target_thread_id")
        if target_chat is not None and target_thread is not None:
            if message.chat.type == ChatType.PRIVATE or int(target_chat) == int(message.chat_id):
                return int(target_chat), int(target_thread)
    return int(message.chat_id), None


async def _stop_poll_publisher(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> tuple[bool, int]:
    """Stop sending new polls while leaving all published polls open."""
    task = channel_poll_tasks.get(chat_id)
    cancelled = bool(task and not task.done())
    if cancelled:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    session = pollquiz_sessions.get(chat_id)
    posted = len((session or {}).get("polls", {}))
    if session:
        session["posting_stopped"] = True
    return cancelled, posted


async def pollstop_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop publishing future polls; already-published polls stay open."""
    message = update.effective_message
    if not message:
        return
    chat_id, thread_id = await _resolve_poll_control_destination(update)
    try:
        try:
            await ctx.bot.delete_message(message.chat_id, message.message_id)
        except Exception:
            pass
        cancelled, posted = await _stop_poll_publisher(ctx, chat_id)
        if cancelled:
            text = f"⏸ <b>Poll publishing stopped.</b> {posted} poll(s) already published remain open. Use /pollclose to close them without results, or /pollfinish to close and publish the leaderboard."
        elif chat_id in pollquiz_sessions:
            text = f"ℹ️ Publishing has already finished or stopped. {posted} poll(s) remain open. Use /pollclose or /pollfinish when you are ready."
        else:
            text = "⚠️ No poll quiz is active in this chat."
        await safe_send_message(ctx, chat_id, text, parse_mode=ParseMode.HTML, thread_id=thread_id)
    except Exception as e:
        logger.error("pollstop_command error: %s", e, exc_info=True)


async def pollclose_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Close every tracked poll without generating or publishing a leaderboard."""
    message = update.effective_message
    if not message:
        return
    chat_id, thread_id = await _resolve_poll_control_destination(update)
    try:
        try:
            await ctx.bot.delete_message(message.chat_id, message.message_id)
        except Exception:
            pass
        await _stop_poll_publisher(ctx, chat_id)
        session = pollquiz_sessions.get(chat_id)
        if not session:
            await safe_send_message(ctx, chat_id, "⚠️ There are no tracked poll quizzes to close.", thread_id=thread_id)
            return
        closed = 0
        for poll in session.get("polls", {}).values():
            message_id = poll.get("message_id")
            if not message_id:
                continue
            try:
                await ctx.bot.stop_poll(chat_id=chat_id, message_id=message_id)
                closed += 1
            except Exception as exc:
                logger.debug("Could not close poll %s in %s: %s", message_id, chat_id, exc)
        session["closed"] = True
        session["finalized"] = True
        pollquiz_sessions.pop(chat_id, None)
        pollquiz_controls.pop(chat_id, None)
        await safe_send_message(ctx, chat_id, f"🔒 Closed {closed} poll(s). No leaderboard was generated.", thread_id=thread_id)
    except Exception as e:
        logger.error("pollclose_command error: %s", e, exc_info=True)


async def pollfinish_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Close polls and explicitly request final scoring/leaderboard."""
    message = update.effective_message
    if not message:
        return
    chat_id, thread_id = await _resolve_poll_control_destination(update)
    try:
        try:
            await ctx.bot.delete_message(message.chat_id, message.message_id)
        except Exception:
            pass
        await _stop_poll_publisher(ctx, chat_id)
        session = pollquiz_sessions.get(chat_id)
        if not session:
            await safe_send_message(ctx, chat_id, "⚠️ There is no poll quiz available to finalize.", thread_id=thread_id)
            return
        await finalize_pollquiz(ctx, chat_id)
        pollquiz_sessions.pop(chat_id, None)
        pollquiz_controls.pop(chat_id, None)
    except Exception as e:
        logger.error("pollfinish_command error: %s", e, exc_info=True)


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
    # Keep familiar aliases from the standalone poll studio.
    application.add_handler(CommandHandler(["pollquiz", "quizpoll", "publish"], pollquiz_channel_command))
    application.add_handler(CommandHandler("polltarget", polltarget_command))
    application.add_handler(CommandHandler("pollstop", pollstop_command))
    application.add_handler(CommandHandler("pollclose", pollclose_command))
    application.add_handler(CommandHandler("pollfinish", pollfinish_command))
