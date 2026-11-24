"""Bot API migration of the creator's batch-management commands."""
from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from quizbot.creator_bot import state
from quizbot.database import BatchRepository, QuizRepository, get_db
from quizbot.runner_bot.handlers.creator import SOURCE_SESSIONS
from quizbot.shared.utils import is_premium_user
from quizbot.shared.callback_utils import batch_callback_target_user_id
from quizbot.shared import config

logger = logging.getLogger(__name__)


def _owner_or_admin(uid: int) -> bool:
    return uid == config.OWNER_ID or uid in config.ADMIN_IDS


async def _premium(message, uid: int) -> bool:
    if await is_premium_user(uid):
        return True
    await message.reply_text("🔒 Premium access is required for batch management. Use /pay to view plans.")
    return False


def _batch_keyboard(batch: dict, uid: int) -> InlineKeyboardMarkup:
    bid = str(batch["batch_id"])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Add chat", callback_data=f"bb:addchat:{bid}:{uid}"), InlineKeyboardButton("➖ Remove chat", callback_data=f"bb:rmchatmenu:{bid}:{uid}")],
        [InlineKeyboardButton("➕ Add quiz", callback_data=f"bb:addquiz:{bid}:{uid}"), InlineKeyboardButton("➖ Remove quiz", callback_data=f"bb:rmquizmenu:{bid}:{uid}")],
        [InlineKeyboardButton("✏️ Edit", callback_data=f"bb:editmenu:{bid}:{uid}"), InlineKeyboardButton("🗑 Delete", callback_data=f"bb:delmenu:{bid}:{uid}")],
        [InlineKeyboardButton("📦 My batches", callback_data=f"bb:list:{uid}")],
    ])


def _batch_text(batch: dict) -> str:
    chats = ", ".join(str(v) for v in batch.get("chats", [])) or "None"
    quizzes = ", ".join(str(v) for v in batch.get("quizzes", [])) or "None"
    return (
        f"📦 <b>{html.escape(str(batch.get('name', 'Untitled')))}</b> · <code>{html.escape(str(batch.get('batch_id', '')))}</code>\n\n"
        f"{html.escape(str(batch.get('description') or 'No description'))}\n"
        f"Contact: {html.escape(str(batch.get('contact_info') or 'Not set'))}\n"
        f"Payment: {html.escape(str(batch.get('payment_link') or 'Not set'))}\n\n"
        f"👥 Authorized chats: <code>{html.escape(chats)}</code>\n"
        f"📚 Quiz IDs: <code>{html.escape(quizzes)}</code>"
    )


async def _show_batch(message, uid: int, bid: str, *, edit: bool = False) -> None:
    repo = BatchRepository(get_db())
    batch = await repo.get(bid)
    if not batch or (batch.get("creator_id") != uid and not _owner_or_admin(uid)):
        text, markup = "❌ Batch not found or access denied.", None
    else:
        text, markup = _batch_text(batch), _batch_keyboard(batch, uid)
    if edit:
        try:
            await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            return
        except Exception:
            logger.debug("Could not edit batch view; sending a new message", exc_info=True)
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def _show_batch_list(message, uid: int, *, edit: bool = False) -> None:
    batches = await BatchRepository(get_db()).list_by_creator(uid)
    rows = [[InlineKeyboardButton("➕ Create batch", callback_data=f"bb:new:{uid}")]]
    for batch in batches[:10]:
        rows.insert(-1, [InlineKeyboardButton(
            f"{str(batch.get('name', 'Batch'))[:28]} · {len(batch.get('quizzes') or [])} quiz(es)",
            callback_data=f"bb:view:{batch['batch_id']}:{uid}",
        )])
    text = f"📦 <b>Your batches</b> · {len(batches)} total"
    if not batches:
        text += "\nYou haven't created a batch yet."
    markup = InlineKeyboardMarkup(rows)
    if edit:
        try:
            await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            return
        except Exception:
            logger.debug("Could not edit batch list; sending a new message", exc_info=True)
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user or not await _premium(message, user.id):
        return
    await _show_batch_list(message, user.id)


async def createbatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user or not await _premium(message, user.id):
        return
    if user.id in state.quiz_creation or user.id in state.edit_sessions or user.id in state.batch_sessions or user.id in SOURCE_SESSIONS:
        await message.reply_text("Finish or cancel your active quiz creation/edit session first.")
        return
    state.batch_sessions[user.id] = {"step": "name"}
    await message.reply_text("📦 <b>Create batch</b>\nSend the batch name.", parse_mode=ParseMode.HTML)


async def searchbatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    term = " ".join(context.args).strip()
    if not message:
        return
    if len(term) < 2:
        await message.reply_text("Usage: /searchbatch KEYWORD (at least 2 characters)")
        return
    batches = await BatchRepository(get_db()).search(term, limit=10)
    if not batches:
        await message.reply_text("🔎 No batches found.")
        return
    lines = [f"📦 <b>Batch search: {html.escape(term)}</b>"]
    for batch in batches:
        lines.append(
            f"\n<b>{html.escape(str(batch.get('name', 'Batch')))}</b> · <code>{batch.get('batch_id')}</code>\n"
            f"{html.escape(str(batch.get('description') or '')[:100])}"
        )
    await message.reply_text("\n".join(lines)[:4000], parse_mode=ParseMode.HTML)


async def batch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    parts = (query.data or "").split(":")
    if len(parts) < 3:
        await query.answer("Invalid batch action.", show_alert=True)
        return
    action = parts[1]
    # editfield callback data is bb:editfield:<batch_id>:<user_id>:<field>,
    # so its user ID is the penultimate item; all other actions put it last.
    try:
        target_uid = batch_callback_target_user_id(parts, action)
    except (ValueError, IndexError):
        await query.answer("Invalid batch action.", show_alert=True)
        return
    if user.id != target_uid:
        await query.answer("This batch menu belongs to another user.", show_alert=True)
        return
    if action == "list":
        await _show_batch_list(query.message, user.id, edit=True)
        await query.answer()
        return
    if action == "new":
        if not await is_premium_user(user.id):
            await query.answer("Premium access required.", show_alert=True)
            return
        if user.id in state.quiz_creation or user.id in state.edit_sessions or user.id in state.batch_sessions or user.id in SOURCE_SESSIONS:
            await query.answer("Finish your active quiz session first.", show_alert=True)
            return
        state.batch_sessions[user.id] = {"step": "name"}
        await query.message.reply_text("📦 Send the batch name.")
        await query.answer()
        return

    # Attach a newly created paid quiz to one of the creator's batches.
    # These actions carry the quiz ID, not a batch ID, so they are handled
    # before the ordinary batch lookup below.
    if action == "attachquiz":
        if len(parts) != 4:
            await query.answer("Invalid quiz reference.", show_alert=True)
            return
        qid = parts[2]
        quiz = await QuizRepository(get_db()).get(qid)
        if not quiz or (quiz.get("creator_id") != user.id and not _owner_or_admin(user.id)):
            await query.answer("Quiz not found or access denied.", show_alert=True)
            return
        batches = await repo.list_by_creator(user.id)
        if not batches:
            if user.id in state.batch_sessions or user.id in state.quiz_creation or user.id in state.edit_sessions or user.id in SOURCE_SESSIONS:
                await query.answer("Finish your active session first.", show_alert=True)
                return
            state.batch_sessions[user.id] = {"step": "name", "_pending_qid": qid}
            await query.answer()
            await query.message.reply_text("📦 You have no batches yet. Send a batch name and this quiz will be attached automatically.")
            return
        rows = [[InlineKeyboardButton(str(batch.get("name", "Batch"))[:35], callback_data=f"bb:doattach:{batch['batch_id']}:{qid}:{user.id}")] for batch in batches[:8]]
        rows.append([InlineKeyboardButton("➕ Create a new batch", callback_data=f"bb:newforquiz:{qid}:{user.id}")])
        await query.message.reply_text("📦 Choose a batch for this quiz:", reply_markup=InlineKeyboardMarkup(rows))
        await query.answer()
        return
    if action == "doattach":
        if len(parts) != 5:
            await query.answer("Invalid attachment reference.", show_alert=True)
            return
        bid, qid = parts[2], parts[3]
        batch = await repo.get(bid)
        if not batch or (batch.get("creator_id") != user.id and not _owner_or_admin(user.id)):
            await query.answer("Batch not found or access denied.", show_alert=True)
            return
        quiz = await QuizRepository(get_db()).get(qid)
        if not quiz or (quiz.get("creator_id") != user.id and not _owner_or_admin(user.id)):
            await query.answer("Quiz not found or access denied.", show_alert=True)
            return
        await repo.add_quiz(bid, qid)
        await query.answer("Quiz attached to batch.")
        await _show_batch(query.message, user.id, bid)
        return
    if action == "newforquiz":
        if len(parts) != 4:
            await query.answer("Invalid quiz reference.", show_alert=True)
            return
        qid = parts[2]
        quiz = await QuizRepository(get_db()).get(qid)
        if not quiz or (quiz.get("creator_id") != user.id and not _owner_or_admin(user.id)):
            await query.answer("Quiz not found or access denied.", show_alert=True)
            return
        if user.id in state.batch_sessions or user.id in state.quiz_creation or user.id in state.edit_sessions or user.id in SOURCE_SESSIONS:
            await query.answer("Finish your active session first.", show_alert=True)
            return
        state.batch_sessions[user.id] = {"step": "name", "_pending_qid": qid}
        await query.message.reply_text("📦 Send the new batch name; the quiz will be attached automatically.")
        await query.answer()
        return

    # Actions below carry a batch ID as the third field.
    if len(parts) < 4:
        await query.answer("Invalid batch action.", show_alert=True)
        return
    bid = parts[2]
    repo = BatchRepository(get_db())
    batch = await repo.get(bid)
    if not batch or (batch.get("creator_id") != user.id and not _owner_or_admin(user.id)):
        await query.answer("Batch not found or access denied.", show_alert=True)
        return

    if action == "view":
        await _show_batch(query.message, user.id, bid, edit=True)
        await query.answer()
    elif action in {"addchat", "addquiz"}:
        state.batch_sessions[user.id] = {"step": "addchat" if action == "addchat" else "addqz", "bid": bid}
        prompt = "Send the numeric group/channel ID (for example -100123456)." if action == "addchat" else "Send the Quiz ID to attach."
        await query.message.reply_text(prompt)
        await query.answer()
    elif action == "rmchatmenu":
        chats = batch.get("chats") or []
        if not chats:
            await query.answer("No authorized chats in this batch.", show_alert=True)
            return
        rows = [[InlineKeyboardButton(str(chat_id), callback_data=f"bb:rmchat:{bid}:{chat_id}:{user.id}")] for chat_id in chats[:20]]
        await query.message.reply_text("Choose a chat to remove:", reply_markup=InlineKeyboardMarkup(rows))
        await query.answer()
    elif action == "rmchat":
        if len(parts) != 5:
            await query.answer("Invalid chat ID.", show_alert=True)
            return
        try:
            chat_id = int(parts[3])
        except ValueError:
            await query.answer("Invalid chat ID.", show_alert=True)
            return
        await repo.remove_chat(bid, chat_id)
        await query.answer("Chat removed.")
        await _show_batch(query.message, user.id, bid)
    elif action == "rmquizmenu":
        quizzes = batch.get("quizzes") or []
        if not quizzes:
            await query.answer("No quizzes attached to this batch.", show_alert=True)
            return
        rows = [[InlineKeyboardButton(str(qid), callback_data=f"bb:rmquiz:{bid}:{qid}:{user.id}")] for qid in quizzes[:20]]
        await query.message.reply_text("Choose a quiz to detach:", reply_markup=InlineKeyboardMarkup(rows))
        await query.answer()
    elif action == "rmquiz":
        if len(parts) != 5:
            await query.answer("Invalid quiz ID.", show_alert=True)
            return
        await repo.remove_quiz(bid, parts[3])
        await query.answer("Quiz detached.")
        await _show_batch(query.message, user.id, bid)
    elif action == "editmenu":
        rows = [
            [InlineKeyboardButton("Name", callback_data=f"bb:editfield:{bid}:{user.id}:name"), InlineKeyboardButton("Description", callback_data=f"bb:editfield:{bid}:{user.id}:description")],
            [InlineKeyboardButton("Contact", callback_data=f"bb:editfield:{bid}:{user.id}:contact_info"), InlineKeyboardButton("Payment", callback_data=f"bb:editfield:{bid}:{user.id}:payment_link")],
        ]
        await query.message.reply_text("Choose the field to edit:", reply_markup=InlineKeyboardMarkup(rows))
        await query.answer()
    elif action == "editfield":
        if len(parts) != 5 or parts[4] not in {"name", "description", "contact_info", "payment_link"}:
            await query.answer("Invalid field.", show_alert=True)
            return
        state.batch_sessions[user.id] = {"step": f"edit_{parts[4]}", "bid": bid}
        await query.message.reply_text(f"Send the new value for {parts[4].replace('_', ' ')}. Send `skip` to clear optional fields.")
        await query.answer()
    elif action == "delmenu":
        rows = [[InlineKeyboardButton("✅ Confirm delete", callback_data=f"bb:delete:{bid}:{user.id}"), InlineKeyboardButton("Cancel", callback_data=f"bb:view:{bid}:{user.id}")]]
        await query.message.reply_text(f"Delete batch {bid}? Its quiz/chat links will also be removed.", reply_markup=InlineKeyboardMarkup(rows))
        await query.answer()
    elif action == "delete":
        await repo.delete(bid)
        await query.answer("Batch deleted.")
        await _show_batch_list(query.message, user.id)
    else:
        await query.answer("Unknown batch action.", show_alert=True)


async def batch_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if not message or not user or not message.text:
        return
    session = state.batch_sessions.get(user.id)
    if not session:
        return
    text = message.text.strip()
    step = session.get("step")
    repo = BatchRepository(get_db())

    if step == "name":
        if not text or len(text) > 80:
            await message.reply_text("Batch name must contain 1–80 characters.")
            return
        session["name"] = text
        session["step"] = "desc"
        await message.reply_text("Send a description, or `skip`.")
    elif step == "desc":
        session["desc"] = None if text.lower() == "skip" else text[:1000]
        session["step"] = "contact"
        await message.reply_text("Send contact information, or `skip`.")
    elif step == "contact":
        session["contact"] = None if text.lower() == "skip" else text[:300]
        session["step"] = "payment"
        await message.reply_text("Send a payment link, or `skip`.")
    elif step == "payment":
        session["payment"] = None if text.lower() == "skip" else text[:500]
        batch = await repo.create(user.id, session["name"], description=session.get("desc"), contact_info=session.get("contact"), payment_link=session.get("payment"))
        pending_qid = session.get("_pending_qid")
        if pending_qid:
            await repo.add_quiz(batch["batch_id"], pending_qid)
        state.batch_sessions.pop(user.id, None)
        await message.reply_text(f"✅ Batch created: <b>{html.escape(session['name'])}</b> · <code>{batch['batch_id']}</code>", parse_mode=ParseMode.HTML)
        await _show_batch(message, user.id, batch["batch_id"])
    elif step == "addchat":
        try:
            chat_id = int(text)
        except ValueError:
            await message.reply_text("Send a numeric Telegram chat ID.")
            return
        await repo.add_chat(session["bid"], chat_id)
        bid = session["bid"]
        state.batch_sessions.pop(user.id, None)
        await message.reply_text("✅ Chat authorized for this batch.")
        await _show_batch(message, user.id, bid)
    elif step == "addqz":
        quiz = await QuizRepository(get_db()).get(text)
        if not quiz:
            await message.reply_text("Quiz ID not found. Please send a valid Quiz ID.")
            return
        if quiz.get("creator_id") != user.id and not _owner_or_admin(user.id):
            await message.reply_text("You can only attach quizzes you own.")
            return
        await repo.add_quiz(session["bid"], text)
        bid = session["bid"]
        state.batch_sessions.pop(user.id, None)
        await message.reply_text("✅ Quiz attached.")
        await _show_batch(message, user.id, bid)
    elif str(step).startswith("edit_"):
        field = str(step)[5:]
        update_value = None if text.lower() == "skip" and field != "name" else text
        field_map = {"name": "name", "description": "description", "contact_info": "contact_info", "payment_link": "payment_link"}
        if field not in field_map or (field == "name" and (not text or len(text) > 80)):
            await message.reply_text("Invalid value. Please try again.")
            return
        await repo.update(session["bid"], **{field_map[field]: update_value})
        bid = session["bid"]
        state.batch_sessions.pop(user.id, None)
        await message.reply_text("✅ Batch updated.")
        await _show_batch(message, user.id, bid)


def register(application: Application) -> None:
    application.add_handler(CommandHandler("batch", batch_command))
    application.add_handler(CommandHandler("createbatch", createbatch_command))
    application.add_handler(CommandHandler("searchbatch", searchbatch_command))
    application.add_handler(CallbackQueryHandler(batch_callback, pattern=r"^bb:"))
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, batch_input),
        group=-4,
    )
