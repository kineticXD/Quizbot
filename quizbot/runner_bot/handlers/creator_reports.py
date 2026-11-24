"""Bot API versions of creator HTML and test-series report commands."""
from __future__ import annotations

import asyncio
import io
import logging
import time

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from quizbot.database import QuizRepository, get_db
from quizbot.shared import config
from quizbot.shared.html.quiz_report import render_quiz_html
from quizbot.shared.utils import is_premium_user
from quizbot.shared.utils.http import get_session, request_json

logger = logging.getLogger(__name__)
_TSR_LOCK = asyncio.Lock()


def _parse_testseries_args(raw: str) -> tuple[str, str, list[str]]:
    mode = "keyonly"
    title = "Mock Test"
    quiz_ids: list[str] = []
    for token in raw.split():
        lower = token.lower()
        if lower.startswith("mode="):
            candidate = lower.split("=", 1)[1]
            if candidate in {"inline", "keyonly"}:
                mode = candidate
        elif lower.startswith("title="):
            title = token.split("=", 1)[1].replace("_", " ")
        else:
            quiz_ids.append(token.strip())
    return mode, title[:100], quiz_ids


async def _generate_pdf_via_api(quizzes: list[dict], mode: str, title: str, poll_timeout: int = 180) -> bytes:
    if not config.PDF_API_BASE:
        raise RuntimeError("PDF_API_BASE is not configured.")
    payload_questions = []
    for quiz in quizzes:
        for question in quiz.get("questions", []):
            options = [str(option) for option in question.get("options", []) if option]
            if not options:
                continue
            payload_questions.append({
                "question": str(question.get("question", "")).strip(),
                "options": options,
                "correct_option_id": question.get("correct_option_id", question.get("correct_option", 0)),
                "explanation": str(question.get("explanation") or "").strip(),
            })
    if not payload_questions:
        raise RuntimeError("No usable questions found in the selected quizzes.")
    base = config.PDF_API_BASE.rstrip("/")
    payload = {
        "questions_json": payload_questions,
        "institute_name": "Quizbot",
        "tagline": "Test Series",
        "exam_title": title,
        "solution_display": "inline" if mode == "inline" else "end",
        "quiz_names": [str(item.get("quiz_name") or item.get("qid") or "") for item in quizzes],
        "async": True,
    }
    status, job = await request_json("POST", f"{base}/api/generate", json_body=payload)
    if status != 200 or not isinstance(job, dict):
        raise RuntimeError(f"PDF service rejected the request (HTTP {status}).")
    if job.get("error"):
        raise RuntimeError(str(job["error"]))
    progress_url = base + str(job.get("progress_url", ""))
    download_url = base + str(job.get("download_url", ""))
    if not progress_url.startswith(base + "/") or not download_url.startswith(base + "/"):
        raise RuntimeError("PDF service returned invalid job URLs.")
    deadline = time.monotonic() + poll_timeout
    while time.monotonic() < deadline:
        _, progress = await request_json("GET", progress_url)
        if not isinstance(progress, dict):
            raise RuntimeError("PDF service returned an invalid progress response.")
        state = progress.get("status")
        if state == "error":
            raise RuntimeError(str(progress.get("error") or "PDF generation failed."))
        if state == "done":
            break
        await asyncio.sleep(1.2)
    else:
        raise RuntimeError("PDF generation timed out. Try fewer quizzes.")
    session = await get_session()
    async with session.get(download_url) as response:
        if response.status != 200:
            raise RuntimeError(f"PDF download failed (HTTP {response.status}).")
        data = await response.read()
    if not data.startswith(b"%PDF"):
        raise RuntimeError("PDF service returned a file that is not a PDF.")
    return data


async def whtml_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    if not context.args:
        await message.reply_text("Usage: /whtml QUIZ_ID")
        return
    qid = context.args[0].strip()
    status = await message.reply_text("Preparing your HTML report…")
    quiz = await QuizRepository(get_db()).get(qid)
    if not quiz:
        await status.edit_text("Quiz not found.")
        return
    if quiz.get("creator_id") != user.id and user.id != config.OWNER_ID and user.id not in config.ADMIN_IDS:
        await status.edit_text("You can only export quizzes you own.")
        return
    try:
        data, filename = await render_quiz_html(quiz, mode="exam")
        buffer = io.BytesIO(data)
        buffer.name = filename
        await context.bot.send_document(
            chat_id=message.chat_id,
            document=buffer,
            filename=filename,
            caption=f"Quiz report: {quiz.get('quiz_name', qid)} · {len(quiz.get('questions', []))} questions",
        )
        await status.delete()
    except Exception as exc:
        logger.exception("Bot API HTML report failed")
        await status.edit_text(f"Could not generate the report: {str(exc)[:180]}")


async def testseries_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user:
        return
    if not config.PDF_API_BASE:
        await message.reply_text("PDF test-series generation is not configured. Use /whtml for an interactive HTML report.")
        return
    if not await is_premium_user(user.id):
        await message.reply_text("🔒 Premium access is required. Use /pay to view plans.")
        return
    if _TSR_LOCK.locked():
        await message.reply_text("Another test-series PDF is being generated. Please try again shortly.")
        return
    raw = " ".join(context.args).strip()
    if not raw:
        await message.reply_text(
            "Usage: /testseries QID1 [QID2 ...] [mode=inline|keyonly] [title=Your_Title]\n"
            "mode=inline adds answers after questions; mode=keyonly puts the answer key at the end."
        )
        return
    mode, title, qids = _parse_testseries_args(raw)
    if not qids:
        await message.reply_text("Please provide at least one quiz ID.")
        return
    async with _TSR_LOCK:
        status = await message.reply_text(f"Fetching {len(qids)} quiz(es)…")
        repo = QuizRepository(get_db())
        quizzes, missing, denied = [], [], []
        for qid in qids[:20]:
            quiz = await repo.get(qid)
            if not quiz or not quiz.get("questions"):
                missing.append(qid)
            elif quiz.get("creator_id") != user.id and user.id != config.OWNER_ID and user.id not in config.ADMIN_IDS:
                denied.append(qid)
            else:
                quizzes.append(quiz)
        if not quizzes:
            await status.edit_text("No accessible quizzes found. Check the IDs and ownership.")
            return
        await status.edit_text(f"Generating PDF for {len(quizzes)} quiz(es)…")
        try:
            data = await _generate_pdf_via_api(quizzes, mode, title)
            filename = "MockTest_" + "_".join(qids[:3]) + ("_etc" if len(qids) > 3 else "") + ".pdf"
            buffer = io.BytesIO(data)
            buffer.name = filename
            caption = f"{title}\nQuizzes: {len(quizzes)} · Questions: {sum(len(q.get('questions', [])) for q in quizzes)}"
            if missing or denied:
                caption += f"\nSkipped: {len(missing) + len(denied)} invalid or inaccessible ID(s)."
            await context.bot.send_document(chat_id=message.chat_id, document=buffer, filename=filename, caption=caption)
            await status.delete()
        except Exception as exc:
            logger.exception("Bot API test-series generation failed")
            await status.edit_text(f"PDF generation failed: {str(exc)[:220]}")


def register(application: Application) -> None:
    application.add_handler(CommandHandler("whtml", whtml_command))
    for name in ("testseries", "tsr", "mocktest"):
        application.add_handler(CommandHandler(name, testseries_command))
