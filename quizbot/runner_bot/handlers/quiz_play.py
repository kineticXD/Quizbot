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
from ..state import channel_poll_tasks, rate_limiter, session_mgr, tasks, translation_mgr
from ..telegram_utils import (
    _get_topic_thread_id,
    prepare_poll_data,
    safe_send_message,
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


def _is_anon_admin(message) -> bool:
    return message.sender_chat is not None or (
        message.from_user is not None and message.from_user.id == _ANON_ADMIN_ID
    )


async def _require_admin(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: Optional[int], is_anon: bool) -> bool:
    """Return True if the caller may run admin-only quiz-control commands."""
    if is_anon:
        return True
    try:
        member = await ctx.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
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
        f"📚 <b>{name}</b> started\n⏱️ Timer: {timer}s\n📋 Q{start_idx}–{end_idx}",
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
            await safe_send_message(
                ctx, chat_id, f"📝 <b>Original</b>\n\n{original_q['question']}", parse_mode=ParseMode.HTML
            )
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
            await safe_send_message(ctx, chat_id, overflow, parse_mode=ParseMode.HTML)
            await asyncio.sleep(0.5)

        timer = s.get("current_section_timer", s["quiz_data"]["timer"])
        timer += s.get("modified_timer_offset", 0)
        # Supports timers up to 240s (4m)
        timer = max(10, min(int(timer), 240))

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


async def end_private_quiz(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
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
            f"🏆 <b>Quiz Completed!</b>\n\n"
            f"📝 Quiz: {qname}\n📊 Total: {total}\n\n"
            f"📈 <b>Your Performance:</b>\n"
            f"✅ Correct: {correct}\n❌ Wrong: {wrong}\n"
            f"🎯 Score: {score:.2f}\n⏱️ Time: {int(minutes)}m {int(seconds)}s\n"
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
            await safe_send_message(ctx, chat_id, overflow, parse_mode=ParseMode.HTML, thread_id=thread_id)
            await asyncio.sleep(0.5)

        timer = _apply_char_boost(q, base_timer) + s.get("modified_timer_offset", 0)
        timer = max(10, min(int(timer), 240))

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
                await safe_send_message(ctx, chat_id, overflow, parse_mode=ParseMode.HTML, thread_id=thread_id)
                await asyncio.sleep(0.5)

            timer = _apply_char_boost(q, sec_timer) + s.get("modified_timer_offset", 0)
            timer = max(10, min(int(timer), 240))

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
    txt = f"💡 <b>Explanation:</b>\n\n{expl}"
    await safe_send_message(ctx, chat_id, txt, parse_mode=ParseMode.HTML, thread_id=thread_id)


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
        for pid, pinfo in s.get("polls", {}).items():
            if pid in pdata.get("answers", {}):
                ans = pdata["answers"][pid]
                if is_correct(ans["option"], pinfo["correct_option"]):
                    corr += 1
                else:
                    wrng += 1
        score = (corr * correct_mark) - (wrng * neg)
        rankings.append((pdata.get("name", "User"), corr, wrng, score))

    rankings.sort(key=lambda r: r[3], reverse=True)
    top = rankings[:5]

    lines = [f"📊 <b>Mid-Quiz Leaderboard</b> ({current_q}/{total_q})\n"]
    for i, (name, c, w, sc) in enumerate(top, 1):
        lines.append(f"{i}. <b>{name}</b> — {sc:.1f} pts ({c}✅ {w}❌)")

    await safe_send_message(ctx, chat_id, "\n".join(lines), parse_mode=ParseMode.HTML, thread_id=thread_id)


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


async def _end_group_quiz(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, update: Any) -> None:
    thread_id = _get_topic_thread_id(update)
    try:
        s = await session_mgr.delete(chat_id)
        if not s:
            return

        quiz_data = s["quiz_data"]
        questions = s["questions"]
        total = len(questions)
        neg = quiz_data.get("negative_marking", 0)
        correct_mark = quiz_data.get("correct_mark", 1)
        participants = s.get("participants", {})

        await safe_send_message(ctx, chat_id, "🏁 <b>Quiz Finished!</b> Calculating final scores...", parse_mode=ParseMode.HTML, thread_id=thread_id)

        user_summary = []
        for uid, udata in participants.items():
            correct = wrong = 0
            total_time = 0.0
            for pid, pinfo in s.get("polls", {}).items():
                if pid in udata.get("answers", {}):
                    ans = udata["answers"][pid]
                    total_time += max(0, ans["time"] - pinfo["sent_time"])
                    if is_correct(ans["option"], pinfo["correct_option"]):
                        correct += 1
                    else:
                        wrong += 1

            score = (correct * correct_mark) - (wrong * neg)
            user_summary.append({
                "user_id": uid, "name": udata.get("name", "Aspirant"),
                "correct": correct, "wrong": wrong, "score": score,
                "total_time": total_time, "answers": udata.get("answers", {}),
            })

        user_summary.sort(key=lambda x: (x["score"], -x["total_time"]), reverse=True)

        chat_title = "Group Chat"
        try:
            chat_obj = await ctx.bot.get_chat(chat_id)
            if chat_obj and chat_obj.title:
                chat_title = chat_obj.title
        except Exception:
            pass

        qname = quiz_data.get("quiz_name", "Unnamed Quiz")
        lines = [f"🏆 <b>Final Leaderboard — {qname}</b>\n📊 Total Questions: {total}\n"]
        for rank, u in enumerate(user_summary[:10], 1):
            med = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else f"{rank}."
            m, sec = divmod(int(u["total_time"]), 60)
            lines.append(f"{med} <b>{u['name']}</b>: {u['score']:.2f} pts ({u['correct']}✅ {u['wrong']}❌) in {m}m {sec}s")

        if len(user_summary) > 10:
            lines.append(f"\n...and {len(user_summary) - 10} more participants!")

        await safe_send_message(ctx, chat_id, "\n".join(lines), parse_mode=ParseMode.HTML, thread_id=thread_id)

        await _record_attempt_and_report(
            ctx, chat_id, quiz_data, user_summary, chat_title=chat_title,
            protect_type=False, thread_id=thread_id,
        )
    except Exception as e:
        logger.error("_end_group_quiz error: %s", e, exc_info=True)
        await safe_send_message(ctx, chat_id, "❌ Error ending quiz.", thread_id=thread_id)


async def _record_attempt_and_report(
    ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, quiz_data: dict, user_summary: list[dict],
    chat_title: str, protect_type: bool, thread_id: Optional[int]
) -> None:
    try:
        db = get_db()
        attempt_repo = AttemptRepository(db)
        lb_repo = LeaderboardRepository(db)
        mistake_repo = MistakeRepository(db)
        qstats_repo = QuestionStatsRepository(db)

        qid = quiz_data.get("question_set_id") or quiz_data.get("qid")
        questions = quiz_data.get("questions", [])

        for u in user_summary:
            uid = u["user_id"]
            if uid <= 0:
                continue

            await attempt_repo.record_attempt(
                user_id=uid, quiz_id=qid, score=u["score"], correct=u["correct"],
                wrong=u["wrong"], total_questions=len(questions), total_time=u["total_time"],
            )

            await lb_repo.upsert_entry(
                quiz_id=qid, user_id=uid, user_name=u["name"], score=u["score"],
                correct=u["correct"], wrong=u["wrong"], total_questions=len(questions),
                time_taken=u["total_time"],
            )

            for pid, ans in u["answers"].items():
                pinfo = quiz_data.get("polls", {}).get(pid, {})
                q_idx = pinfo.get("question_index")
                if q_idx is not None and q_idx < len(questions):
                    q = questions[q_idx]
                    corr = is_correct(ans["option"], pinfo.get("correct_option", []))
                    if not corr:
                        await mistake_repo.add_mistake(uid, qid, q_idx, q)
                    await qstats_repo.record_answer(qid, q_idx, corr)

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
    except Exception as e:
        logger.error("_record_attempt_and_report error: %s", e, exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════
# POLL ANSWER ROUTER & HANDLERS
# ═══════════════════════════════════════════════════════════════════════════

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


def register(application: Application) -> None:
    application.add_handler(PollAnswerHandler(poll_answer_handler))
