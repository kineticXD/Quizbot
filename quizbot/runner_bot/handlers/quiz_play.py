"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from html import escape
import tempfile
import time
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Poll, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, PollAnswerHandler

from quizbot.database import (
    AttemptRepository,
    LeaderboardRepository,
    MistakeRepository,
    QuestionStatsRepository,
    QuizRepository,
    get_db,
)
from quizbot.shared import config
from quizbot.shared.leaderboard_format import format_leaderboard_markdown, format_leaderboard_plain
from quizbot.shared.timers import clamp_quiz_timer, format_timer, validate_quiz_timer
from quizbot.shared.html.quiz_report import render_quiz_html
from quizbot.shared.mini_app_link import mini_app_web_app_button_ptb
from quizbot.shared.rich_quiz import (
    RichDispatchResult,
    _is_rich,
    _normalise_math_spacing,
    enrich_question_dispatch,
    send_rich_or_fallback,
)
from quizbot.shared.utils import is_premium_user

from ..pdf_reports import render_quiz_pdf
from ..quiz_utils import (
    get_section_for_question,
    is_correct,
    resolve_quiz_access,
    section_marks,
    shuffle_options_multi,
)
from ..state import channel_poll_tasks, pending_quiz_settings, rate_limiter, session_mgr, tasks, translation_mgr
from ..telegram_utils import (
    _get_topic_thread_id,
    prepare_poll_data,
    safe_send_message,
    safe_send_long_message,
    safe_send_poll,
    send_raw_api,
)

logger = logging.getLogger(__name__)

_ANON_ADMIN_ID = 1087968824  # Telegram's fake @GroupAnonymousBot user id.

# How often (every N questions) to auto-post a mid-quiz leaderboard. 0 disables it.
MID_QUIZ_LB_INTERVAL = 10

# Anti-cheat pattern-detection tuning
CHEAT_CHECK_EVERY = 10
CHEAT_WRONG_RATIO = 0.5


async def _send_leaderboard_table(
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    title: str,
    rows: list[dict],
    *,
    total_questions: int = 0,
    status: str = "Final Results",
    subtitle: Optional[str] = None,
    thread_id: Optional[int] = None,
    limit: int = 20,
) -> bool:
    """Publish a rich Telegram leaderboard table with a readable fallback.

    Uses the same sendRichMessage Markdown-table route as the Creator Bot's
    leaderboard. Telegram clients render it as a real table/card rather than
    the old plain list. If rich messages are unavailable, send an accessible
    text fallback with accuracy and timing intact.
    """
    rich_text = format_leaderboard_markdown(
        title, rows, total_questions=total_questions, status=status,
        subtitle=subtitle, limit=limit,
    )
    plain_text = format_leaderboard_plain(
        title, rows, total_questions=total_questions, status=status, limit=limit,
    )
    fallback_state = {"sent": False}

    async def _plain_fallback(_text: str):
        message = await safe_send_message(
            ctx, chat_id, plain_text, thread_id=thread_id,
        )
        fallback_state["sent"] = message is not None
        return message

    rich_ok = await send_rich_or_fallback(
        lambda method, params: send_raw_api(ctx, method, params),
        _plain_fallback,
        chat_id,
        rich_text,
        thread_id=thread_id,
        plain_limit=3500,
    )
    return bool(rich_ok or fallback_state["sent"])


def _is_anon_admin(message) -> bool:
    return message.sender_chat is not None or (
        message.from_user is not None and message.from_user.id == _ANON_ADMIN_ID
    )


async def _require_admin(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: Optional[int], is_anon: bool) -> bool:
    """Return True for configured bot owners/admins or Telegram group admins.

    OWNER_ID/ADMIN_IDS are global bot-control identities. They must not be
    forced to also hold Telegram administrator status in every group where
    the bot is installed. Group-local administrators remain supported.
    """
    if is_anon:
        return True
    if user_id is None:
        return False
    if user_id == config.OWNER_ID or user_id in config.ADMIN_IDS:
        return True
    try:
        member = await ctx.bot.get_chat_member(chat_id, user_id)
        status = getattr(member.status, "value", member.status)
        return str(status).lower() in ("administrator", "creator")
    except Exception:
        return False


async def translate_text(text: str, target_lang: str) -> str:
    """Translate text via deep-translator (lazy import)."""
    try:
        from deep_translator import GoogleTranslator
    except ImportError:
        logger.warning("deep_translator not installed; translation skipped")
        return text
    if not text or not text.strip():
        return text
    try:
        max_len = 5000
        if len(text) <= max_len:
            return GoogleTranslator(source="auto", target=target_lang).translate(text)
        parts, current = [], ""
        for para in text.split("\n"):
            if len(current) + len(para) + 1 <= max_len:
                current = f"{current}\n{para}" if current else para
            else:
                if current:
                    parts.append(current)
                current = para
        if current:
            parts.append(current)
        translated = [
            GoogleTranslator(source="auto", target=target_lang).translate(p) for p in parts if p.strip()
        ]
        return "\n".join(translated)
    except Exception as e:
        logger.error("Translation error: %s", e)
        return text


async def translate_question(qdata: dict, target_lang: str) -> dict:
    t = qdata.copy()
    try:
        if "question" in qdata:
            t["question"] = await translate_text(qdata["question"], target_lang)
        if isinstance(qdata.get("options"), list):
            t["options"] = [await translate_text(o, target_lang) for o in qdata["options"]]
        if qdata.get("reply_text"):
            t["reply_text"] = await translate_text(qdata["reply_text"], target_lang)
        if qdata.get("explanation"):
            t["explanation"] = await translate_text(qdata["explanation"], target_lang)
    except Exception as e:
        logger.error("translate_question error: %s", e)
        return qdata
    return t


async def wait_until_resumed(chat_id: int) -> None:
    while True:
        s = session_mgr.get(chat_id)
        if not s or not s.get("paused"):
            return
        await asyncio.sleep(1.5)


def _apply_char_boost(q: dict, timer: int) -> int:
    """Long questions get a little extra reading time when the timer is
    already short: >600 chars -> +30s, >450 chars -> +20s."""
    total_chars = len(q.get("question", ""))
    for opt in q.get("options", []):
        total_chars += len(opt)
    total_chars += len(q.get("reply_text") or "")
    if timer < 30:
        if total_chars > 600:
            timer += 30
        elif total_chars > 450:
            timer += 20
    return timer


# ═══════════════════════════════════════════════════════════════════════════
# PRIVATE (DM) QUIZ
# ═══════════════════════════════════════════════════════════════════════════

async def start_private_quiz(
    chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, questions: list[dict], quiz: dict, qid: str, skip: int = 0
) -> None:
    """Begin a one-on-one DM quiz session."""
    try:
        data = {
            "quiz_id": qid, "current_index": skip, "paused": False,
            "questions": questions, "quiz_data": quiz, "is_private": True,
            "participants": {chat_id: {"name": "You", "answers": {}, "start_time": time.time()}},
            "waiting_for_answer": False, "active_poll_id": None,
            "polls": {}, "section_msgs": [], "current_section": None,
            "sections": quiz.get("sections", []), "context": ctx,
            "modified_timer_offset": 0,
        }
        await session_mgr.create(chat_id, data)

        sections = quiz.get("sections", [])
        if sections:
            sections.sort(key=lambda s: s["question_range"][0])
            start_sec = next((sec for sec in sections if skip < sec["question_range"][1]), None)
            if start_sec:
                await _private_section_start(chat_id, start_sec, skip)
            else:
                await safe_send_message(ctx, chat_id, "⚠️ Skip count beyond all sections.")
                await session_mgr.delete(chat_id)
        else:
            await send_private_question(chat_id, ctx, skip)
    except Exception as e:
        logger.error("start_private_quiz error: %s", e, exc_info=True)
        await safe_send_message(ctx, chat_id, "❌ Error starting quiz.")
        await session_mgr.delete(chat_id)


async def _private_section_start(chat_id: int, section: dict, skip: int = 0) -> None:
    s = session_mgr.get(chat_id)
    if not s or s.get("paused"):
        return
    start_idx, end_idx = section["question_range"]
    name = section.get("name", f"Section {start_idx}-{end_idx}")
    timer = section.get("timer", s["quiz_data"]["timer"])
    ctx = s["context"]
    msg = await safe_send_message(
        ctx, chat_id,
        f"📚 <b>{name}</b> started\n⏱️ Timer: {format_timer(timer)}\n📋 Q{start_idx}–{end_idx}",
        parse_mode=ParseMode.HTML,
    )
    if msg:
        s["section_msgs"].append(msg.message_id)
        s["current_section"] = section
        s["current_section_timer"] = timer
        await session_mgr.update(chat_id, s)
        first = max(skip, start_idx - 1)
        await send_private_question(chat_id, ctx, first)


async def send_private_question(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, idx: int) -> None:
    try:
        s = session_mgr.get(chat_id)
        if not s:
            return
        await wait_until_resumed(chat_id)

        questions = s["questions"]
        if idx >= len(questions):
            await end_private_quiz(chat_id, ctx)
            return

        cur_section = s.get("current_section")
        if cur_section and idx + 1 > cur_section["question_range"][1] - 1:
            s["is_last_in_section"] = True
            await session_mgr.update(chat_id, s)

        q = questions[idx]
        original_q = q.copy()

        target_lang = translation_mgr.get_language(chat_id)
        if target_lang:
            q = await translate_question(q, target_lang)

        options = q["options"]
        correct_id = q["correct_option_id"]
        correct_ids = correct_id if isinstance(correct_id, list) else [correct_id]
        is_multi = len(correct_ids) > 1
        file_id = q.get("file_id")
        reply_text = q.get("reply_text")
        do_shuffle = s["quiz_data"].get("shuffle_options", False)
        shuffle_o_count = s["quiz_data"].get("shuffle_options_count", 0)

        if do_shuffle:
            options, correct_ids = shuffle_options_multi(options, correct_ids, shuffle_o_count)

        if target_lang and target_lang != "en":
            await safe_send_long_message(ctx, chat_id, f"📝 Original\n\n{original_q['question']}")
            await asyncio.sleep(0.5)

        photo_msg_id = None
        if file_id:
            try:
                photo_msg = await ctx.bot.send_photo(chat_id=chat_id, photo=file_id)
                photo_msg_id = photo_msg.message_id
                await asyncio.sleep(0.3)
            except Exception:
                pass

        _tid = s.get("message_thread_id")
        rich_res: RichDispatchResult = await enrich_question_dispatch(
            lambda method, params: send_raw_api(ctx, method, params),
            lambda text: safe_send_message(ctx, chat_id, text, parse_mode=ParseMode.HTML),
            chat_id, q, idx, len(questions), thread_id=_tid,
        )
        if rich_res.rich_sent:
            await asyncio.sleep(0.5)

        _q_text = rich_res.poll_question_override or q["question"]
        _rt = None if rich_res.suppress_reply_text else reply_text
        poll_q, poll_opts, poll_expl, overflow, poll_desc = prepare_poll_data(
            _q_text, options, correct_ids[0], q.get("explanation"), _rt, idx, len(questions)
        )
        if rich_res.poll_options_override:
            poll_opts = rich_res.poll_options_override
        if rich_res.suppress_description:
            poll_desc = None
            overflow = None

        if overflow:
            await safe_send_long_message(ctx, chat_id, overflow)
            await asyncio.sleep(0.5)

        timer = s.get("timer_override") or s.get("current_section_timer", s["quiz_data"]["timer"])
        timer = clamp_quiz_timer(int(timer) + s.get("modified_timer_offset", 0))

        poll_kwargs: dict[str, Any] = {}
        if is_multi:
            poll_kwargs["correct_option_ids"] = correct_ids
            poll_kwargs["allows_multiple_answers"] = True
        else:
            poll_kwargs["correct_option_id"] = correct_ids[0]
        if photo_msg_id:
            poll_kwargs["reply_to_message_id"] = photo_msg_id
        if poll_desc:
            poll_kwargs["description"] = poll_desc

        protect = not (chat_id == s["quiz_data"].get("creator_id"))

        poll_msg = await safe_send_poll(
            ctx, chat_id, question=poll_q, options=poll_opts, type=Poll.QUIZ,
            explanation=poll_expl, is_anonymous=False, open_period=timer,
            protect_content=protect, **poll_kwargs,
        )

        if poll_msg:
            s["active_poll_id"] = poll_msg.poll.id
            s["active_poll_message_id"] = poll_msg.message_id
            s["waiting_for_answer"] = True
            s["poll_start_time"] = time.time()
            s["current_index"] = idx
            s["polls"][poll_msg.poll.id] = {
                "correct_option": correct_ids, "sent_time": time.time(), "question_index": idx,
            }
            await session_mgr.update(chat_id, s)
            tasks.spawn(_private_timeout(chat_id, poll_msg.poll.id, timer), name=f"quiz_{chat_id}_pvt_timeout")
        elif idx + 1 < len(questions):
            await asyncio.sleep(2)
            await send_private_question(chat_id, ctx, idx + 1)
        else:
            await end_private_quiz(chat_id, ctx)
    except Exception as e:
        logger.error("send_private_question error: %s", e, exc_info=True)
        s = session_mgr.get(chat_id)
        if s and idx + 1 < len(s.get("questions", [])):
            await asyncio.sleep(2)
            await send_private_question(chat_id, ctx, idx + 1)


async def _private_timeout(chat_id: int, poll_id: str, timer: int) -> None:
    await asyncio.sleep(timer + 2)
    s = session_mgr.get(chat_id)
    if not s or s.get("active_poll_id") != poll_id:
        return
    s["waiting_for_answer"] = False
    cur = s.get("current_index", 0)
    s["current_index"] = cur + 1
    await session_mgr.update(chat_id, s)

    ctx = s.get("context")
    if ctx and s.get("quiz_data", {}).get("show_explanation"):
        await _send_explanation_after_poll(ctx, chat_id, s["questions"][cur], thread_id=s.get("message_thread_id"))

    if s.get("is_last_in_section"):
        s["is_last_in_section"] = False
        await session_mgr.update(chat_id, s)
        await _end_private_section(chat_id)
    elif ctx and cur + 1 < len(s.get("questions", [])):
        await asyncio.sleep(1)
        await send_private_question(chat_id, ctx, cur + 1)
    else:
        await end_private_quiz(chat_id, ctx)


async def handle_private_poll_answer(poll_id: str, user_id: int, option_ids: list, current_time: float) -> None:
    for chat_id in list(session_mgr.sessions.keys()):
        s = session_mgr.get(chat_id)
        if not s or not s.get("is_private") or poll_id != s.get("active_poll_id"):
            continue

        if user_id not in s["participants"]:
            s["participants"][user_id] = {"name": "You", "answers": {}}
        s["participants"][user_id]["answers"][poll_id] = {"option": option_ids, "time": current_time}
        s["waiting_for_answer"] = False
        cur = s.get("current_index", 0)
        await session_mgr.update(chat_id, s)

        ctx = s.get("context")
        if ctx and s.get("quiz_data", {}).get("show_explanation"):
            await _send_explanation_after_poll(ctx, chat_id, s["questions"][cur], thread_id=s.get("message_thread_id"))

        if s.get("is_last_in_section"):
            s["is_last_in_section"] = False
            await session_mgr.update(chat_id, s)
            await asyncio.sleep(1.5)
            await _end_private_section(chat_id)
        else:
            await asyncio.sleep(1.5)
            if ctx and cur + 1 < len(s.get("questions", [])):
                await send_private_question(chat_id, ctx, cur + 1)
            else:
                await end_private_quiz(chat_id, ctx)
        return


async def _end_private_section(chat_id: int) -> None:
    s = session_mgr.get(chat_id)
    if not s:
        return
    ctx = s.get("context")
    if s.get("section_msgs"):
        try:
            await ctx.bot.unpin_chat_message(chat_id, s["section_msgs"][-1])
        except Exception:
            pass
    sections = s.get("sections", [])
    cur_sec = s.get("current_section")
    if cur_sec and sections:
        cur_end = cur_sec["question_range"][1]
        nxt = next((sec for sec in sections if sec["question_range"][0] > cur_end), None)
        if nxt:
            await _private_section_start(chat_id, nxt, 0)
            return
    await end_private_quiz(chat_id, ctx)


async def end_private_quiz(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, ended_early: bool = False) -> None:
    try:
        s = await session_mgr.delete(chat_id)
        if not s:
            return
        quiz_data = s["quiz_data"]
        questions = s["questions"]
        total = len(questions)
        neg = quiz_data.get("negative_marking", 0)
        correct_mark = quiz_data.get("correct_mark", 1)

        await safe_send_message(ctx, chat_id, "📊 Calculating results...")

        udata = s["participants"].get(chat_id, {})
        correct = wrong = 0
        total_time = 0.0
        for pid, pinfo in s.get("polls", {}).items():
            if pid in udata.get("answers", {}):
                ans = udata["answers"][pid]
                total_time += ans["time"] - pinfo["sent_time"]
                if is_correct(ans["option"], pinfo["correct_option"]):
                    correct += 1
                else:
                    wrong += 1

        score = (correct * correct_mark) - (wrong * neg)
        pct = (correct / total * 100) if total else 0
        acc = (correct / (correct + wrong) * 100) if (correct + wrong) else 0
        minutes, seconds = divmod(total_time, 60)

        qid = quiz_data["question_set_id"]
        buttons = [[InlineKeyboardButton("🔄 Restart", url=f"https://t.me/share/url?url=/start {qid}")]]

        qname = quiz_data.get("quiz_name", "Unnamed Quiz")
        txt = (
            f"🏁 <b>{'Quiz Ended Early' if ended_early else 'Quiz Completed!'}</b>\n\n"
            f"📝 Quiz: {escape(str(qname))}\n📊 Total: {total}\n\n"
            f"📈 <b>Your Performance:</b>\n"
            f"✅ Correct: {correct}\n❌ Wrong: {wrong}\n"
            f"🎯 Score: {score:.2f}\n⏱️ Time: {int(minutes)}m {int(seconds)}s\n"
            f"📝 Answered: {correct + wrong}/{total}\n"
            f"📊 Percentage: {pct:.1f}%\n🎯 Accuracy: {acc:.1f}%"
        )
        await safe_send_message(ctx, chat_id, txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))

        await _record_attempt_and_report(
            ctx, chat_id, quiz_data, [{
                "user_id": chat_id, "name": "You", "correct": correct, "wrong": wrong,
                "score": score, "total_time": total_time, "answers": udata.get("answers", {}),
            }], chat_title="Direct Message", protect_type=False, thread_id=None,
        )
    except Exception as e:
        logger.error("end_private_quiz error: %s", e, exc_info=True)
        await safe_send_message(ctx, chat_id, "❌ Error generating results.")


# ═══════════════════════════════════════════════════════════════════════════
# GROUP QUIZ
# ═══════════════════════════════════════════════════════════════════════════

async def run_group_quiz(
    chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, questions: list[dict], quiz: dict,
    protect_type: bool, update: Any, skip: int = 0,
) -> None:
    """Main group quiz loop -- one long-lived coroutine per running quiz."""
    from ..state import pollquiz_sessions
    pollquiz_sessions.pop(chat_id, None)
    try:
        sections = quiz.get("sections", [])
        if sections:
            sections.sort(key=lambda s: s["question_range"][0])
            await _run_sectioned_quiz(chat_id, ctx, questions, quiz, sections, protect_type, update, skip)
        else:
            await _run_flat_quiz(chat_id, ctx, questions, quiz, protect_type, update, skip)
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.error("run_group_quiz fatal error chat=%s: %s", chat_id, e, exc_info=True)
        await safe_send_message(ctx, chat_id, f"❌ Quiz error: {e}")


async def _run_flat_quiz(
    chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, questions: list[dict], quiz: dict,
    protect: bool, update: Any, skip: int,
) -> None:
    thread_id = _get_topic_thread_id(update)
    base_timer = quiz.get("timer", 30)

    for idx in range(skip, len(questions)):
        s = session_mgr.get(chat_id)
        if not s:
            return
        await wait_until_resumed(chat_id)

        q = questions[idx]
        original_q = q.copy()

        target_lang = translation_mgr.get_language(chat_id)
        if target_lang:
            q = await translate_question(q, target_lang)

        options = q["options"]
        correct_id = q["correct_option_id"]
        correct_ids = correct_id if isinstance(correct_id, list) else [correct_id]
        is_multi = len(correct_ids) > 1
        file_id = q.get("file_id")
        reply_text = q.get("reply_text")
        do_shuffle = quiz.get("shuffle_options", False)
        shuffle_o_count = quiz.get("shuffle_options_count", 0)

        if do_shuffle:
            options, correct_ids = shuffle_options_multi(options, correct_ids, shuffle_o_count)

        if target_lang and target_lang != "en":
            await safe_send_long_message(ctx, chat_id, f"📝 Original\n\n{original_q['question']}", thread_id=thread_id)
            await asyncio.sleep(0.5)

        photo_msg_id = None
        if file_id:
            try:
                photo_msg = await ctx.bot.send_photo(chat_id=chat_id, photo=file_id, message_thread_id=thread_id)
                photo_msg_id = photo_msg.message_id
                await asyncio.sleep(0.3)
            except Exception:
                pass

        rich_res: RichDispatchResult = await enrich_question_dispatch(
            lambda method, params: send_raw_api(ctx, method, params),
            lambda text: safe_send_message(ctx, chat_id, text, parse_mode=ParseMode.HTML, thread_id=thread_id),
            chat_id, q, idx, len(questions), thread_id=thread_id,
        )
        if rich_res.rich_sent:
            await asyncio.sleep(0.5)

        _q_text = rich_res.poll_question_override or q["question"]
        _rt = None if rich_res.suppress_reply_text else reply_text
        poll_q, poll_opts, poll_expl, overflow, poll_desc = prepare_poll_data(
            _q_text, options, correct_ids[0], q.get("explanation"), _rt, idx, len(questions)
        )
        if rich_res.poll_options_override:
            poll_opts = rich_res.poll_options_override
        if rich_res.suppress_description:
            poll_desc = None
            overflow = None

        if overflow:
            await safe_send_long_message(ctx, chat_id, overflow, thread_id=thread_id)
            await asyncio.sleep(0.5)

        if s.get("timer_override") is not None:
            timer_base = int(s["timer_override"])
        else:
            timer_base = _apply_char_boost(q, base_timer)
        timer = clamp_quiz_timer(timer_base + s.get("modified_timer_offset", 0))

        poll_kwargs: dict[str, Any] = {}
        if is_multi:
            poll_kwargs["correct_option_ids"] = correct_ids
            poll_kwargs["allows_multiple_answers"] = True
        else:
            poll_kwargs["correct_option_id"] = correct_ids[0]
        if photo_msg_id:
            poll_kwargs["reply_to_message_id"] = photo_msg_id
        if poll_desc:
            poll_kwargs["description"] = poll_desc

        poll_msg = await safe_send_poll(
            ctx, chat_id, question=poll_q, options=poll_opts, type=Poll.QUIZ,
            explanation=poll_expl, is_anonymous=False, open_period=timer,
            protect_content=protect, message_thread_id=thread_id, **poll_kwargs,
        )

        if poll_msg:
            s["active_poll_id"] = poll_msg.poll.id
            s["active_poll_message_id"] = poll_msg.message_id
            s["current_index"] = idx
            s["polls"][poll_msg.poll.id] = {
                "correct_option": correct_ids, "sent_time": time.time(), "question_index": idx,
            }
            await session_mgr.update(chat_id, s)
            await asyncio.sleep(timer + 1)

            if quiz.get("show_explanation"):
                await _send_explanation_after_poll(ctx, chat_id, q, thread_id=thread_id)

            if MID_QUIZ_LB_INTERVAL > 0 and (idx + 1) % MID_QUIZ_LB_INTERVAL == 0 and (idx + 1) < len(questions):
                await _send_mid_quiz_leaderboard(ctx, chat_id, quiz, idx + 1, len(questions), thread_id)
                await asyncio.sleep(2)
        else:
            await asyncio.sleep(2)

    await _end_group_quiz(chat_id, ctx, update)


async def _run_sectioned_quiz(
    chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, questions: list[dict], quiz: dict,
    sections: list[dict], protect: bool, update: Any, skip: int,
) -> None:
    thread_id = _get_topic_thread_id(update)
    start_sec_idx = 0
    for idx, sec in enumerate(sections):
        if skip < sec["question_range"][1]:
            start_sec_idx = idx
            break

    for sec_idx in range(start_sec_idx, len(sections)):
        s = session_mgr.get(chat_id)
        if not s:
            return
        sec = sections[sec_idx]
        s_start, s_end = sec["question_range"]
        sec_q_indices = list(range(max(skip, s_start - 1), s_end))
        if not sec_q_indices:
            continue

        sec_name = sec.get("name", f"Section {s_start}–{s_end}")
        sec_mode = sec.get("mode", "perpoll")
        sec_timer = sec.get("timer", quiz.get("timer", 30))

        intro = (
            f"📚 <b>{sec_name}</b> started!\n"
            f"📋 Questions {s_start}–{s_end} ({len(sec_q_indices)} total)\n"
        )
        if sec_mode == "slot":
            slot_mins = sec.get("slot_minutes", 10)
            intro += f"🕐 <b>Section Slot Time:</b> {slot_mins} minutes for all questions"
        else:
            intro += f"⏱️ <b>Timer:</b> {sec_timer}s per question"

        sec_msg = await safe_send_message(ctx, chat_id, intro, parse_mode=ParseMode.HTML, thread_id=thread_id)
        if sec_msg:
            try:
                await ctx.bot.pin_chat_message(chat_id, sec_msg.message_id, disable_notification=True)
                s["section_msgs"].append(sec_msg.message_id)
                await session_mgr.update(chat_id, s)
            except Exception:
                pass

        await asyncio.sleep(1.5)

        for q_idx in sec_q_indices:
            s = session_mgr.get(chat_id)
            if not s:
                return
            await wait_until_resumed(chat_id)

            q = questions[q_idx]
            original_q = q.copy()

            target_lang = translation_mgr.get_language(chat_id)
            if target_lang:
                q = await translate_question(q, target_lang)

            options = q["options"]
            correct_id = q["correct_option_id"]
            correct_ids = correct_id if isinstance(correct_id, list) else [correct_id]
            is_multi = len(correct_ids) > 1
            file_id = q.get("file_id")
            reply_text = q.get("reply_text")
            do_shuffle = quiz.get("shuffle_options", False)
            shuffle_o_count = quiz.get("shuffle_options_count", 0)

            if do_shuffle:
                options, correct_ids = shuffle_options_multi(options, correct_ids, shuffle_o_count)

            if target_lang and target_lang != "en":
                await safe_send_message(
                    ctx, chat_id, f"📝 <b>Original</b>\n\n{original_q['question']}",
                    parse_mode=ParseMode.HTML, thread_id=thread_id,
                )
                await asyncio.sleep(0.5)

            photo_msg_id = None
            if file_id:
                try:
                    photo_msg = await ctx.bot.send_photo(chat_id=chat_id, photo=file_id, message_thread_id=thread_id)
                    photo_msg_id = photo_msg.message_id
                    await asyncio.sleep(0.3)
                except Exception:
                    pass

            rich_res: RichDispatchResult = await enrich_question_dispatch(
                lambda method, params: send_raw_api(ctx, method, params),
                lambda text: safe_send_message(ctx, chat_id, text, parse_mode=ParseMode.HTML, thread_id=thread_id),
                chat_id, q, q_idx, len(questions), thread_id=thread_id,
            )
            if rich_res.rich_sent:
                await asyncio.sleep(0.5)

            _q_text = rich_res.poll_question_override or q["question"]
            _rt = None if rich_res.suppress_reply_text else reply_text
            poll_q, poll_opts, poll_expl, overflow, poll_desc = prepare_poll_data(
                _q_text, options, correct_ids[0], q.get("explanation"), _rt, q_idx, len(questions)
            )
            if rich_res.poll_options_override:
                poll_opts = rich_res.poll_options_override
            if rich_res.suppress_description:
                poll_desc = None
                overflow = None

            if overflow:
                await safe_send_long_message(ctx, chat_id, overflow, thread_id=thread_id)
                await asyncio.sleep(0.5)

            if s.get("timer_override") is not None:
                timer_base = int(s["timer_override"])
            else:
                timer_base = _apply_char_boost(q, sec_timer)
            timer = clamp_quiz_timer(timer_base + s.get("modified_timer_offset", 0))

            poll_kwargs: dict[str, Any] = {}
            if is_multi:
                poll_kwargs["correct_option_ids"] = correct_ids
                poll_kwargs["allows_multiple_answers"] = True
            else:
                poll_kwargs["correct_option_id"] = correct_ids[0]
            if photo_msg_id:
                poll_kwargs["reply_to_message_id"] = photo_msg_id
            if poll_desc:
                poll_kwargs["description"] = poll_desc

            poll_msg = await safe_send_poll(
                ctx, chat_id, question=poll_q, options=poll_opts, type=Poll.QUIZ,
                explanation=poll_expl, is_anonymous=False, open_period=timer,
                protect_content=protect, message_thread_id=thread_id, **poll_kwargs,
            )

            if poll_msg:
                s["active_poll_id"] = poll_msg.poll.id
                s["active_poll_message_id"] = poll_msg.message_id
                s["current_index"] = q_idx
                s["polls"][poll_msg.poll.id] = {
                    "correct_option": correct_ids, "sent_time": time.time(), "question_index": q_idx,
                }
                await session_mgr.update(chat_id, s)
                await asyncio.sleep(timer + 1)

                if quiz.get("show_explanation"):
                    await _send_explanation_after_poll(ctx, chat_id, q, thread_id=thread_id)
            else:
                await asyncio.sleep(2)

        if sec_msg:
            try:
                await ctx.bot.unpin_chat_message(chat_id, sec_msg.message_id)
            except Exception:
                pass

        await safe_send_message(ctx, chat_id, f"✅ <b>{sec_name} completed!</b>", parse_mode=ParseMode.HTML, thread_id=thread_id)
        await asyncio.sleep(1)

    await _end_group_quiz(chat_id, ctx, update)


async def _send_explanation_after_poll(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, question: dict, thread_id: Optional[int] = None
) -> None:
    expl = question.get("explanation")
    if not expl or not str(expl).strip():
        return
    txt = f"💡 Explanation:\n\n{expl}"
    await safe_send_long_message(ctx, chat_id, txt, thread_id=thread_id)


async def _send_mid_quiz_leaderboard(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, quiz: dict, current_q: int, total_q: int, thread_id: Optional[int]
) -> None:
    s = session_mgr.get(chat_id)
    if not s:
        return
    parts = s.get("participants", {})
    if not parts:
        return

    neg = quiz.get("negative_marking", 0)
    correct_mark = quiz.get("correct_mark", 1)
    rankings = []

    for uid, pdata in parts.items():
        corr = wrng = 0
        total_time = 0.0
        for pid, pinfo in s.get("polls", {}).items():
            ans = pdata.get("answers", {}).get(pid)
            if not ans:
                continue
            total_time += max(
                0.0,
                float(ans.get("time", 0) or 0) - float(pinfo.get("sent_time", 0) or 0),
            )
            if is_correct(ans.get("option", []), pinfo.get("correct_option", [])):
                corr += 1
            else:
                wrng += 1
        score = (corr * correct_mark) - (wrng * neg)
        rankings.append((pdata.get("name", "User"), corr, wrng, score, total_time))

    rankings.sort(key=lambda r: (r[3], -r[4]), reverse=True)
    rows = [
        {"name": name, "correct": correct, "wrong": wrong, "score": score, "total_time": total_time}
        for name, correct, wrong, score, total_time in rankings
    ]
    await _send_leaderboard_table(
        ctx, chat_id, "Quiz Standings", rows,
        total_questions=total_q, status=f"Live · Question {current_q}/{total_q}",
        subtitle=f"Live standings · {current_q} of {total_q} questions posted",
        thread_id=thread_id, limit=5,
    )


async def handle_group_poll_answer(poll_id: str, user_id: int, option_ids: list, current_time: float, user_name: str) -> None:
    for chat_id in list(session_mgr.sessions.keys()):
        s = session_mgr.get(chat_id)
        if not s or s.get("is_private"):
            continue
        if poll_id not in s.get("polls", {}):
            continue

        if user_id not in s["participants"]:
            s["participants"][user_id] = {"name": user_name, "answers": {}}
        elif user_name and s["participants"][user_id].get("name") != user_name:
            s["participants"][user_id]["name"] = user_name

        s["participants"][user_id]["answers"][poll_id] = {"option": option_ids, "time": current_time}
        await session_mgr.update(chat_id, s)
        return


async def _end_group_quiz(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, update: Any, ended_early: bool = False) -> None:
    """Finalize a group quiz and publish its leaderboard exactly once.

    Older launch paths (setup wizard, AI quiz, PDF quiz) stored questions
    inside quiz_data but did not duplicate them at session["questions"].
    Read both shapes so stopping a quiz cannot fail with KeyError. Keep the
    session until the leaderboard is sent and persistence succeeds, allowing
    an administrator to retry after a transient Telegram/MongoDB failure.
    """
    thread_id = _get_topic_thread_id(update)
    s = session_mgr.get(chat_id)
    if not s:
        return
    if s.get("finalizing"):
        logger.info("Quiz finalization already in progress for chat=%s", chat_id)
        return

    s["finalizing"] = True
    try:
        # Allow the last poll_answer update to be dispatched before scoring.
        await asyncio.sleep(1.5)
        s = session_mgr.get(chat_id) or s
        quiz_data = s.get("quiz_data") or s.get("quiz") or {}
        questions = s.get("questions") or quiz_data.get("questions") or []
        if not questions:
            raise ValueError("The active quiz session has no question data")

        total = len(questions)
        neg = float(quiz_data.get("negative_marking", quiz_data.get("negative_marks", 0)) or 0)
        correct_mark = float(quiz_data.get("correct_mark", quiz_data.get("correct_marks", 1)) or 1)
        participants = s.get("participants", {}) or {}
        polls = s.get("polls", {}) or {}

        status = "Quiz Ended Early" if ended_early else "Quiz Finished!"
        if not s.get("ending_status_sent"):
            await safe_send_message(
                ctx, chat_id, f"🏁 <b>{status}</b> Calculating final scores...",
                parse_mode=ParseMode.HTML, thread_id=thread_id,
            )
            s["ending_status_sent"] = True

        user_summary = []
        for uid, udata in participants.items():
            correct = wrong = 0
            total_time = 0.0
            answers = udata.get("answers", {}) or {}
            for pid, pinfo in polls.items():
                ans = answers.get(pid)
                if not ans:
                    continue
                answer_time = float(ans.get("time", pinfo.get("sent_time", 0)) or 0)
                sent_time = float(pinfo.get("sent_time", answer_time) or 0)
                total_time += max(0.0, answer_time - sent_time)
                if is_correct(ans.get("option", []), pinfo.get("correct_option", [])):
                    correct += 1
                else:
                    wrong += 1

            score = (correct * correct_mark) - (wrong * neg)
            user_summary.append({
                "user_id": int(uid), "name": udata.get("name", "Aspirant"),
                "correct": correct, "wrong": wrong, "score": score,
                "total_time": total_time, "answers": answers,
            })

        user_summary.sort(key=lambda x: (float(x["score"]), -float(x["total_time"])), reverse=True)

        chat_title = "Group Chat"
        try:
            chat_obj = await ctx.bot.get_chat(chat_id)
            if chat_obj and chat_obj.title:
                chat_title = chat_obj.title
        except Exception:
            logger.debug("Could not read chat title for quiz results in %s", chat_id, exc_info=True)

        quiz_name = quiz_data.get("quiz_name", "Unnamed Quiz")
        board_label = "Partial Leaderboard" if ended_early else "Final Leaderboard"
        quiz_ref = quiz_data.get("question_set_id") or quiz_data.get("qid") or s.get("quiz_id")
        if not s.get("leaderboard_message_sent"):
            sent = await _send_leaderboard_table(
                ctx, chat_id, str(quiz_name), user_summary,
                total_questions=total,
                status="Partial Results" if ended_early else "Final Results",
                subtitle=(
                    f"{board_label} · {total} questions · "
                    f"{len(user_summary)} participants"
                ),
                thread_id=thread_id,
                limit=20,
            )
            if not sent:
                # The rich sender has already attempted its plain fallback. A
                # False result means Telegram did not confirm any delivery.
                raise RuntimeError("Telegram did not confirm delivery of the leaderboard")
            s["leaderboard_message_sent"] = True
            if quiz_ref:
                await safe_send_message(
                    ctx, chat_id,
                    f"📌 Saved results can be reopened with /leaderboard {escape(str(quiz_ref))}.",
                    thread_id=thread_id,
                )

        if user_summary and not s.get("leaderboard_persisted"):
            quiz_data_for_report = dict(quiz_data)
            quiz_data_for_report["questions"] = questions
            quiz_data_for_report["polls"] = polls
            persisted = await _record_attempt_and_report(
                ctx, chat_id, quiz_data_for_report, user_summary,
                chat_title=chat_title, protect_type=False, thread_id=thread_id,
            )
            if not persisted:
                # Preserve the session so /stop can retry persistence without
                # duplicating the already-published leaderboard.
                s["finalizing"] = False
                return
            s["leaderboard_persisted"] = True

        await session_mgr.delete(chat_id)
    except Exception as exc:
        logger.error("_end_group_quiz error for chat=%s: %s", chat_id, exc, exc_info=True)
        s["finalizing"] = False
        await safe_send_message(
            ctx, chat_id,
            "❌ I couldn't finish the results safely. Your quiz data has been kept; "
            "an owner/admin can run /stop again after checking the bot logs.",
            thread_id=thread_id,
        )


async def _record_attempt_and_report(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, quiz_data: dict, user_summary: list[dict],
    chat_title: str, protect_type: bool, thread_id: Optional[int]
) -> bool:
    """Persist completed scores, best leaderboard entries, mistakes and stats."""
    if not user_summary:
        logger.warning("No participant scores to persist for chat=%s", chat_id)
        return False
    try:
        db = get_db()
        attempt_repo = AttemptRepository(db)
        lb_repo = LeaderboardRepository(db)
        mistake_repo = MistakeRepository(db)
        qstats_repo = QuestionStatsRepository(db)

        qid = quiz_data.get("question_set_id") or quiz_data.get("qid")
        if not qid:
            logger.error("Cannot persist leaderboard: quiz has no question_set_id/qid")
            return False
        qid = str(qid)
        questions = quiz_data.get("questions", [])
        polls = quiz_data.get("polls", {})
        question_totals: dict[int, dict[str, int]] = {}

        for user in user_summary:
            uid = int(user.get("user_id") or 0)
            if uid <= 0:
                continue

            await attempt_repo.record_attempt(
                user_id=uid, quiz_id=qid, score=user["score"], correct=user["correct"],
                wrong=user["wrong"], total_questions=len(questions), total_time=user["total_time"],
            )
            await lb_repo.upsert_entry(
                quiz_id=qid, user_id=uid, user_name=user["name"], score=user["score"],
                correct=user["correct"], wrong=user["wrong"], total_questions=len(questions),
                time_taken=user["total_time"],
            )

            mistakes = []
            for poll_id, answer in user.get("answers", {}).items():
                poll_info = polls.get(poll_id, {})
                q_idx = poll_info.get("question_index")
                if q_idx is None or not 0 <= int(q_idx) < len(questions):
                    continue
                q_idx = int(q_idx)
                is_answer_correct = is_correct(
                    answer.get("option", []), poll_info.get("correct_option", [])
                )
                stats = question_totals.setdefault(q_idx, {"index": q_idx, "wrong": 0, "total": 0})
                stats["total"] += 1
                if not is_answer_correct:
                    stats["wrong"] += 1
                    mistakes.append({"qid": qid, "index": q_idx})
            if mistakes:
                await mistake_repo.record(uid, mistakes)

        if question_totals:
            await qstats_repo.bulk_update_wrong_stats(qid, list(question_totals.values()))

        web_app_url = f"{config.MINI_APP_DOMAIN}/report?quiz_id={qid}" if config.MINI_APP_DOMAIN else None
        kb_buttons = []
        if web_app_url:
            btn = mini_app_web_app_button_ptb("📊 Detailed Analytics", web_app_url)
            if btn:
                kb_buttons.append([btn])

        reply_markup = InlineKeyboardMarkup(kb_buttons) if kb_buttons else None
        await safe_send_message(
            ctx, chat_id, "🎉 <b>Results saved to leaderboards!</b>",
            parse_mode=ParseMode.HTML, reply_markup=reply_markup, thread_id=thread_id
        )
        return True
    except Exception as e:
        logger.error("_record_attempt_and_report error: %s", e, exc_info=True)
        await safe_send_message(
            ctx, chat_id,
            "⚠️ The final score was calculated, but saving leaderboard records failed. Please check the bot logs.",
            thread_id=thread_id,
        )
        return False


# ═══════════════════════════════════════════════════════════════════════════
# POLL ANSWER ROUTER & HANDLERS
# ═══════════════════════════════════════════════════════════════════════════

async def start_quiz_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Launch the quiz setup wizard from a Telegram deep link: /start QUIZ_ID [skip]."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return

    args = list(ctx.args or [])
    # Payment deep links are handled by the creator-side payment callback.
    if args and args[0].startswith("pay_"):
        return
    if not args:
        await safe_send_message(
            ctx,
            chat.id,
            "<b>Welcome to Advance Quizbot!</b>\n\n"
            "Create quizzes with /create, or open a shared quiz using /start QUIZ_ID.\n"
            "Use /help to see all available commands.",
            parse_mode=ParseMode.HTML,
        )
        return

    qid = args[0].strip()
    try:
        skip = int(args[1]) if len(args) > 1 else 0
    except (TypeError, ValueError):
        await safe_send_message(ctx, chat.id, "Usage: /start QUIZ_ID [skip_count]")
        return
    if skip < 0:
        await safe_send_message(ctx, chat.id, "Skip count cannot be negative.")
        return

    if session_mgr.get(chat.id) or chat.id in pending_quiz_settings:
        await safe_send_message(ctx, chat.id, "⚠️ A quiz or quiz setup is already active here. Finish it or use /stop first.")
        return
    if not await rate_limiter.check(user.id):
        await safe_send_message(ctx, chat.id, "⏱️ Too many requests. Please wait a moment.")
        return

    quiz = await QuizRepository(get_db()).get(qid)
    if not quiz or not quiz.get("questions"):
        await safe_send_message(ctx, chat.id, "❌ Quiz not found or it contains no questions.")
        return
    if skip >= len(quiz["questions"]):
        await safe_send_message(ctx, chat.id, f"❌ Skip count must be between 0 and {len(quiz['questions']) - 1}.")
        return

    chat_type = getattr(chat.type, "value", str(chat.type)).lower()
    if chat_type.startswith("chattype."):
        chat_type = chat_type.split(".", 1)[1]
    if chat_type in ("group", "supergroup") and not await is_premium_user(user.id):
        await safe_send_message(ctx, chat.id, "🔒 Group quiz hosting requires premium access.")
        return

    allowed, batch = await resolve_quiz_access(
        qid, quiz, chat.id, chat_type, user.id, ctx=ctx
    )
    if not allowed:
        if batch:
            details = ["🔒 <b>Quiz access required</b>", f"Batch: <b>{escape(batch.get('name', ''))}</b>"]
            if batch.get("payment_link"):
                details.append(f"Payment: {escape(batch['payment_link'])}")
            if batch.get("contact_info"):
                details.append(f"Contact: {escape(batch['contact_info'])}")
            await safe_send_message(ctx, chat.id, "\n".join(details), parse_mode=ParseMode.HTML)
        else:
            await safe_send_message(
                ctx, chat.id,
                f"🔒 This is a paid quiz. Contact the creator (ID: {quiz.get('creator_id', 'unknown')}) for access.",
            )
        return

    quiz["question_set_id"] = quiz.get("qid", qid)
    quiz["negative_marking"] = quiz.get("negative_marks", quiz.get("negative_marking", 0))
    quiz["correct_mark"] = quiz.get("correct_marks", quiz.get("correct_mark", 1))
    pending_quiz_settings[chat.id] = {
        "quiz": quiz,
        "update": update,
        "skip": skip,
        "protect": chat_type in ("group", "supergroup"),
        "chat_type": chat_type,
        "correct_mark": float(quiz["correct_mark"] or 1),
        "neg_mark": float(quiz["negative_marking"] or 0),
        "shuffle_q": False,
        "shuffle_o": False,
        "shuffle_o_count": 0,
        "show_explanation": bool(quiz.get("show_explanation", False)),
        "timer_override": None,
        "anti_cheat": False,
        "initiator_id": user.id,
        "message_thread_id": getattr(message, "message_thread_id", None),
    }
    from .setup_wizard import show_correct_mark_prompt

    await show_correct_mark_prompt(ctx, chat.id)


async def _authorize_quiz_control(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> tuple[int, dict] | None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if not chat or not user or not message:
        return None

    session = session_mgr.get(chat.id)
    if not session:
        await safe_send_message(ctx, chat.id, "⚠️ No active quiz in this chat.")
        return None

    if chat.type == ChatType.PRIVATE:
        if user.id != chat.id:
            await safe_send_message(ctx, chat.id, "❌ You cannot control this quiz.")
            return None
    elif not await _require_admin(ctx, chat.id, user.id, _is_anon_admin(message)):
        await safe_send_message(ctx, chat.id, "🔒 Only group admins can control this quiz.")
        return None

    return chat.id, session


async def pause_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    authorized = await _authorize_quiz_control(update, ctx)
    if not authorized:
        return
    chat_id, session = authorized
    if session.get("paused"):
        await safe_send_message(ctx, chat_id, "⏸️ The quiz is already paused.")
        return
    session["paused"] = True
    await session_mgr.update(chat_id, session)
    await safe_send_message(ctx, chat_id, "⏸️ Quiz paused. Use /resume to continue.")


async def resume_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    authorized = await _authorize_quiz_control(update, ctx)
    if not authorized:
        return
    chat_id, session = authorized
    if not session.get("paused"):
        await safe_send_message(ctx, chat_id, "▶️ The quiz is not paused.")
        return
    session["paused"] = False
    await session_mgr.update(chat_id, session)
    await safe_send_message(ctx, chat_id, "▶️ Quiz resumed.")


async def stop_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat and user and message and chat.id in pending_quiz_settings and not session_mgr.get(chat.id):
        # /stop should also cancel the setup wizard before the first poll is
        # sent; otherwise a half-configured quiz blocks /start indefinitely.
        pending = pending_quiz_settings.get(chat.id, {})
        if chat.type == ChatType.PRIVATE:
            allowed = user.id == chat.id
        else:
            allowed = user.id == pending.get("initiator_id") or await _require_admin(
                ctx, chat.id, user.id, _is_anon_admin(message)
            )
        if not allowed:
            await safe_send_message(ctx, chat.id, "🔒 Only the quiz initiator or a group admin can cancel this setup.")
            return
        pending_quiz_settings.pop(chat.id, None)
        await safe_send_message(ctx, chat.id, "🛑 Quiz setup cancelled.")
        return

    authorized = await _authorize_quiz_control(update, ctx)
    if not authorized:
        return
    chat_id, session = authorized
    poll_message_id = session.get("active_poll_message_id")
    if poll_message_id:
        try:
            await ctx.bot.stop_poll(chat_id=chat_id, message_id=poll_message_id)
        except Exception as exc:
            logger.debug("Unable to close active poll on stop: %s", exc)
    # Stop the question loop, but preserve the session long enough to calculate
    # and publish the partial leaderboard instead of discarding everyone's work.
    tasks.cancel_all_for_chat(chat_id)
    if session.get("is_private"):
        await end_private_quiz(chat_id, ctx, ended_early=True)
    else:
        await _end_group_quiz(chat_id, ctx, update, ended_early=True)


async def _change_speed(update: Update, ctx: ContextTypes.DEFAULT_TYPE, delta: int | None) -> None:
    authorized = await _authorize_quiz_control(update, ctx)
    if not authorized:
        return
    chat_id, session = authorized
    if delta is None:
        session["timer_override"] = None
        session["modified_timer_offset"] = 0
        await session_mgr.update(chat_id, session)
        await safe_send_message(ctx, chat_id, "⏱️ Timer reset to the quiz and section defaults for upcoming questions.")
        return

    base = session.get("timer_override") or session.get("current_section_timer") or session.get("quiz_data", {}).get("timer", 30)
    current_offset = int(session.get("modified_timer_offset", 0))
    effective = clamp_quiz_timer(int(base) + current_offset + int(delta))
    session["modified_timer_offset"] = effective - int(base)
    await session_mgr.update(chat_id, session)
    await safe_send_message(ctx, chat_id, f"⏱️ Timer set to {format_timer(effective)} for upcoming questions.")


async def timer_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Set an exact per-question timer for the remainder of the active quiz."""
    chat = update.effective_chat
    if not chat:
        return
    if len(ctx.args or []) != 1:
        await safe_send_message(ctx, chat.id, "Usage: <code>/timer 90</code>\nSet 10 seconds to 60 minutes for upcoming questions.", parse_mode=ParseMode.HTML)
        return
    try:
        seconds = validate_quiz_timer(ctx.args[0])
    except ValueError as exc:
        await safe_send_message(ctx, chat.id, f"❌ {exc}")
        return

    authorized = await _authorize_quiz_control(update, ctx)
    if not authorized:
        return
    chat_id, session = authorized
    session["timer_override"] = seconds
    session["modified_timer_offset"] = 0
    await session_mgr.update(chat_id, session)
    await safe_send_message(
        ctx, chat_id,
        f"⏱️ Timer changed to <b>{format_timer(seconds)}</b> per question from the next question. The currently open poll is unchanged.",
        parse_mode=ParseMode.HTML,
    )


async def slow_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _change_speed(update, ctx, 5)


async def fast_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _change_speed(update, ctx, -5)


async def normal_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _change_speed(update, ctx, None)


async def leaderboard_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show a rich leaderboard for the active or a previously saved quiz."""
    chat = update.effective_chat
    if not chat:
        return

    session = session_mgr.get(chat.id)
    if session and not session.get("is_private") and not ctx.args:
        quiz = session.get("quiz_data", {})
        participants = session.get("participants", {})
        polls = session.get("polls", {})
        correct_mark = float(quiz.get("correct_mark", quiz.get("correct_marks", 1)) or 1)
        negative_marking = float(quiz.get("negative_marking", quiz.get("negative_marks", 0)) or 0)
        rows: list[dict] = []
        for participant in participants.values():
            correct = wrong = 0
            total_time = 0.0
            answers = participant.get("answers", {})
            for poll_id, poll_data in polls.items():
                answer = answers.get(poll_id)
                if not answer:
                    continue
                elapsed = max(0.0, float(answer.get("time", 0)) - float(poll_data.get("sent_time", 0)))
                total_time += elapsed
                if is_correct(answer.get("option", []), poll_data.get("correct_option", [])):
                    correct += 1
                else:
                    wrong += 1
            rows.append({
                "name": participant.get("name", "Aspirant"),
                "score": correct * correct_mark - wrong * negative_marking,
                "correct": correct, "wrong": wrong, "total_time": total_time,
            })
        rows.sort(key=lambda item: (item["score"], -item["total_time"]), reverse=True)
        if not rows:
            await safe_send_message(ctx, chat.id, "📊 No answers have been recorded yet.")
            return
        await _send_leaderboard_table(
            ctx, chat.id, str(quiz.get("quiz_name", "Quiz")), rows,
            total_questions=len(quiz.get("questions", [])), status="Live Standings",
            subtitle=f"Live standings · {len(quiz.get('questions', []))} questions · {len(rows)} participants",
            thread_id=_get_topic_thread_id(update), limit=20,
        )
        return

    from ..state import pollquiz_sessions
    pollquiz_session = pollquiz_sessions.get(chat.id)
    if pollquiz_session and not ctx.args:
        from .poll_quiz import pollquiz_score_rows
        poll_rows = pollquiz_score_rows(pollquiz_session)
        if not poll_rows:
            await safe_send_message(ctx, chat.id, "📊 No answers have been recorded yet. Participants must answer the individual polls in this group.")
            return
        await _send_leaderboard_table(
            ctx, chat.id, str(pollquiz_session.get("quiz_name", "Poll Quiz")), poll_rows,
            total_questions=pollquiz_session.get("total_questions", 0),
            status="Live Standings" if not pollquiz_session.get("finalized") else "Final Results",
            subtitle=(
                f"{len(pollquiz_session.get('polls', {}))}/{pollquiz_session.get('total_questions', 0)} "
                f"questions posted · {len(poll_rows)} participants"
            ),
            thread_id=pollquiz_session.get("message_thread_id"), limit=20,
        )
        return

    qid = ctx.args[0].strip() if ctx.args else None
    if not qid:
        await safe_send_message(ctx, chat.id, "Usage: /leaderboard QUIZ_ID after a quiz has finished, or /leaderboard during an active quiz.")
        return
    try:
        rows_db = await LeaderboardRepository(get_db()).top(qid, limit=20)
        quiz_doc = await QuizRepository(get_db()).get(qid)
    except Exception as exc:
        logger.error("leaderboard query failed for %s: %s", qid, exc, exc_info=True)
        await safe_send_message(ctx, chat.id, "⚠️ Could not load the saved leaderboard right now.")
        return
    if not rows_db:
        await safe_send_message(ctx, chat.id, f"📊 No saved scores found for quiz {escape(qid)} yet.")
        return
    quiz_doc = quiz_doc or {}
    rows = [{
        "name": entry.get("user_name") or entry.get("username") or "Aspirant",
        "score": entry.get("score", 0), "correct": entry.get("correct", 0),
        "wrong": entry.get("wrong", 0), "time_taken": entry.get("time_taken", 0),
    } for entry in rows_db]
    await _send_leaderboard_table(
        ctx, chat.id, str(quiz_doc.get("quiz_name") or qid), rows,
        total_questions=len(quiz_doc.get("questions") or []), status="Saved Leaderboard",
        subtitle=f"Saved results · {len(quiz_doc.get('questions') or [])} questions · {len(rows)} participants",
        thread_id=_get_topic_thread_id(update), limit=20,
    )


async def poll_answer_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    answer = update.poll_answer
    if not answer:
        return

    poll_id = answer.poll_id
    user_id = answer.user.id
    user_name = answer.user.full_name or "Aspirant"
    option_ids = list(answer.option_ids)
    current_time = time.time()

    await handle_private_poll_answer(poll_id, user_id, option_ids, current_time)
    await handle_group_poll_answer(poll_id, user_id, option_ids, current_time, user_name)
    # The non-expiring /pollquiz flow has its own per-chat poll registry.
    # Import lazily to avoid a handler-module import cycle.
    from .poll_quiz import handle_pollquiz_poll_answer
    await handle_pollquiz_poll_answer(poll_id, user_id, option_ids, current_time, user_name)


def register(application: Application) -> None:
    application.add_handler(CommandHandler("start", start_quiz_command))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("slow", slow_command))
    application.add_handler(CommandHandler("fast", fast_command))
    application.add_handler(CommandHandler("normal", normal_command))
    application.add_handler(CommandHandler("timer", timer_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(PollAnswerHandler(poll_answer_handler))
