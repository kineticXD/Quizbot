"""
Advance Quiz Bot — Open Source Project
This project was originally developed by Gagan (github.com/devgaganin).
Reference: https://t.me/advance_quiz_bot
The codebase has been reviewed and verified with the assistance of Claude AI.
"""

from __future__ import annotations

import asyncio
import logging
import time

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from quizbot.database import (
    LeaderboardRepository,
    QuizRepository,
    UserRepository,
    get_db,
)
from quizbot.shared import config
from quizbot.shared.rich_quiz import send_rich_or_fallback
from quizbot.shared.utils.http import request_json

from .. import state
from ..premium_grant import grant_and_notify
from ..ratelimit import ratelimit

logger = logging.getLogger(__name__)

HELP_PAGES: dict[str, str] = {
    "home": (
        "🎓 **QUIZBOT CONTROL CENTRE**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Your workspace to create, organise, share and run quizzes.\n\n"
        "Choose a section below. Each section has the commands and examples you need.\n\n"
        "💡 **Quick start**\n"
        "Create: `/create`  ·  Library: `/myquizzes`  ·  Play: `/start QUIZ_ID`"
    ),
    "create": (
        "🧩 **CREATE & MANAGE**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Build a quiz, add questions and manage your saved sets.\n\n"
        "➕ **Create**\n"
        "`/create` — start a new quiz\n"
        "`/done` — finish and save it\n"
        "`/cancel` — discard the current creation\n\n"
        "🛠 **Manage**\n"
        "`/edit <quiz_id>` — open the quiz editor\n"
        "`/stopedit` — leave the editor\n"
        "`/del <quiz_id>` — delete one of your quizzes\n"
        "`/info <quiz_id>` — view quiz details\n"
        "`/setpromo <text>` — set a promo message on your quizzes\n\n"
        "📥 You can add questions by sending text, forwarding quiz polls, or uploading a supported `.txt` file."
    ),
    "library": (
        "📚 **YOUR QUIZ LIBRARY**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Find and reopen the quizzes you have created.\n\n"
        "`/myquizzes` — show your quiz library\n"
        "`/myquizzes <term>` — search your own quizzes\n"
        "`/info <quiz_id>` — inspect a quiz\n"
        "`/edit <quiz_id>` — edit questions and settings\n"
        "`/del <quiz_id>` — remove a quiz\n\n"
        "Use the **Discover** section to search public quizzes or share your own."
    ),
    "discover": (
        "🔎 **DISCOVER & SHARE**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Search public quizzes and share them with a group.\n\n"
        "`/search <word>` — search public quizzes\n"
        "`/quiz <word>` — shortcut for public search\n"
        "`/leaders <quiz_id>` — full quiz leaderboard\n"
        "`/aspirants <quiz_id>` — leaderboard shortcut\n\n"
        "🔗 **Sharing**\n"
        "Open a quiz from `/myquizzes` and use its Start link, or share the quiz ID and launch it with `/start QUIZ_ID`."
    ),
    "access": (
        "📦 **BATCHES & ACCESS**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Bundle quizzes and control access to paid quiz sets.\n\n"
        "📦 **Batches**\n"
        "`/batch` — manage your batches\n"
        "`/createbatch` — create a batch\n"
        "`/searchbatch <term>` — find a batch\n\n"
        "🔐 **Paid quiz access**\n"
        "`/add <chat_id>` — authorise a chat\n"
        "`/rem <chat_id>` — revoke chat access\n"
        "`/remall` — clear paid-quiz authorisations\n"
        "`/auth` — view authorised chats\n"
        "`/removeuser <user_id>` — revoke a user's premium access (owner only)"
    ),
    "player": (
        "🎮 **QUIZ PLAYER**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Launch and control quizzes in private chats and groups.\n\n"
        "🚀 **Launch & control**\n"
        "`/start <id> [skip]` — launch a quiz\n"
        "`/pause` · `/resume` · `/stop` — control a session\n"
        "`/leaderboard` — view the live group leaderboard\n\n"
        "⏱ **Timing**\n"
        "`/slow` · `/fast` — adjust upcoming questions\n"
        "`/normal` — restore the quiz timer\n"
        "`/timer <seconds>` — set 10–3600 seconds\n\n"
        "🧪 **Other modes**\n"
        "`/pollquiz <quiz_id>` · `/pollstop` — non-expiring poll mode\n"
        "`/mix <count> <id1> <id2> ...` — mix quiz sets\n"
        "`/schedule` · `/viewschedule` · `/cancelschedule` — schedule a group quiz"
    ),
    "reports": (
        "📊 **REPORTS & RESULTS**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Review results and create shareable reports.\n\n"
        "`/whtml <quiz_id>` — interactive HTML quiz report\n"
        "`/testseries <ids>` — generate a PDF test series (when configured)\n"
        "`/html` — configure HTML result reports\n"
        "`/pdf` — configure PDF result reports\n"
        "`/leaders <quiz_id>` — view the full leaderboard\n\n"
        "Reports are generated from recorded quiz attempts; available output depends on the quiz and deployment configuration."
    ),
    "ai": (
        "🤖 **AI, PDF & IMPORT TOOLS**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Create question sets faster with AI and document imports.\n\n"
        "✨ **AI quiz**\n"
        "`/aiquiz <topic>` — generate a quiz from a topic\n"
        "`/setkey <provider> <key>` — add an AI provider key\n"
        "`/mykeys` — list your saved AI keys\n"
        "`/delkey <provider>` — remove an AI key\n\n"
        "📄 **PDF & imports**\n"
        "`/pdfquiz` — reply to a PDF to create a quiz\n"
        "Send question text, forward a quiz poll, or upload a `.txt` file while creating a quiz.\n\n"
        "🌐 `/trans <lang>` — translate quiz questions\n"
        "🧹 `/remove <words>` · `/mywords` · `/clearlist` — manage import word filters"
    ),
    "account": (
        "⚙️ **ACCOUNT & SETTINGS**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Manage premium access and creator preferences.\n\n"
        "`/pay` — buy or renew premium\n"
        "`/settings` — creator settings and quick-save defaults\n"
        "`/limit` — view your command rate-limit status\n"
        "`/features` — view the feature overview\n"
        "`/help` — reopen this control centre\n\n"
        "Need help? Send the relevant command and its error message when contacting the project maintainer."
    ),
    "features": (
        "✨ **BUILT FOR ADVANCED QUIZZING**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "• Manual, bulk-text, forwarded-poll and PDF question imports\n"
        "• AI-assisted quiz generation with configurable providers\n"
        "• Standard, sectional and marathon quiz modes\n"
        "• Per-question timers, including custom durations up to 60 minutes\n"
        "• Negative marking, shuffle controls and quiz editing\n"
        "• Public discovery, inline query sharing and quiz batches\n"
        "• Free and paid access controls\n"
        "• Live leaderboards, HTML reports and PDF test series\n"
        "• Group scheduling, translation and a Telegram Mini App\n\n"
        "Choose a section from the menu to see how to use each feature."
    ),
    "owner": (
        "🛠 **OWNER & ADMIN TOOLS**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "These commands are restricted to configured owners or admins.\n\n"
        "`/statses` — platform usage statistics\n"
        "`/testapi` — test database connectivity\n"
        "`/gcast` — broadcast a replied-to message\n"
        "`/stopcast` — stop an active broadcast\n"
        "`/listquiz` — list platform quizzes\n"
        "`/ban <quiz_id>` — ban a quiz\n"
        "`/delall` — delete all quizzes\n"
        "`/convertall` — convert paid quizzes to free\n\n"
        "⚠️ Use destructive actions carefully."
    ),
}

_HELP_HOME_ROWS = [
    [("➕ Create & Manage", "create"), ("📚 My Quizzes", "library")],
    [("🔎 Discover & Share", "discover"), ("📦 Batches & Access", "access")],
    [("🎮 Quiz Player", "player"), ("📊 Reports", "reports")],
    [("🤖 AI & Imports", "ai"), ("⚙️ Account", "account")],
    [("✨ Features", "features")],
]


def _help_keyboard(page: str, user_id: int) -> InlineKeyboardMarkup:
    """Build the compact inline navigation for the help control centre."""
    rows: list[list[InlineKeyboardButton]] = []
    if page == "home":
        for row in _HELP_HOME_ROWS:
            rows.append([
                InlineKeyboardButton(label, callback_data=f"hmenu:{target}")
                for label, target in row
            ])
        if _is_owner_or_admin(user_id):
            rows.append([InlineKeyboardButton("🛠 Owner Tools", callback_data="hmenu:owner")])
    else:
        related = {
            "create": [("📚 My Quizzes", "library"), ("🤖 AI & Imports", "ai")],
            "library": [("➕ Create & Manage", "create"), ("🔎 Discover", "discover")],
            "discover": [("📚 My Quizzes", "library"), ("📊 Reports", "reports")],
            "access": [("📚 My Quizzes", "library"), ("⚙️ Account", "account")],
            "player": [("📊 Reports", "reports"), ("🔎 Discover", "discover")],
            "reports": [("🎮 Quiz Player", "player"), ("📚 My Quizzes", "library")],
            "ai": [("➕ Create & Manage", "create"), ("⚙️ Account", "account")],
            "account": [("💎 Features", "features"), ("📦 Batches", "access")],
            "features": [("➕ Create & Manage", "create"), ("🎮 Quiz Player", "player")],
            "owner": [("📊 Reports", "reports"), ("⚙️ Account", "account")],
        }
        destinations = related.get(page, [])
        if destinations:
            rows.append([
                InlineKeyboardButton(label, callback_data=f"hmenu:{target}")
                for label, target in destinations
            ])
    rows.append([
        InlineKeyboardButton("⌂ Main Menu", callback_data="hmenu:home"),
        InlineKeyboardButton("✖ Close", callback_data="hmenu:close"),
    ])
    return InlineKeyboardMarkup(rows)


async def help_menu_callback(c: Client, cb: CallbackQuery) -> None:
    """Navigate the /help control centre without sending a new message."""
    page = (cb.data or "").partition(":")[2]
    if page == "close":
        try:
            if cb.message:
                await cb.message.delete()
            await cb.answer("Menu closed.")
        except Exception:
            await cb.answer()
        return

    if page not in HELP_PAGES:
        await cb.answer("This menu button has expired. Send /help to reopen it.", show_alert=True)
        return
    if page == "owner" and not _is_owner_or_admin(cb.from_user.id):
        await cb.answer("Owner/admin access only.", show_alert=True)
        return
    if not cb.message:
        await cb.answer()
        return

    try:
        await cb.message.edit_text(
            HELP_PAGES[page],
            reply_markup=_help_keyboard(page, cb.from_user.id),
            disable_web_page_preview=True,
        )
    except Exception as exc:
        # A rapid double tap can produce MessageNotModified; it is harmless.
        logger.debug("Help menu update failed for page=%s: %s", page, exc)
    await cb.answer()



@ratelimit("default")
async def start_cmd(c: Client, m: Message) -> None:
    """/start -- silent unless it's a Razorpay payment deep-link
    (`?start=pay_<token>`).
    """
    uid = m.from_user.id
    await UserRepository(get_db()).get_or_create(uid)

    args = m.text.split(maxsplit=1)
    param = args[1].strip() if len(args) > 1 else ""

    if param.startswith("pay_"):
        token = param[4:]
        entry = state.pending_payments.get(token)
        if not entry:
            await m.reply(
                "Payment session expired or not found.\n"
                "If you completed payment, contact support with /help."
            )
            return
        if entry["uid"] != uid:
            await m.reply("This payment link belongs to a different account.")
            return
        if time.time() > entry["expires_at"]:
            state.pending_payments.pop(token, None)
            await m.reply("Session expired (15 min limit). Use /pay to start again.")
            return

        state.pending_payments.pop(token, None)
        days = entry["days"]
        try:
            fmt = await grant_and_notify(uid, days)
        except Exception:
            logger.error("grant_and_notify failed for uid=%s token=%s", uid, token, exc_info=True)
            state.pending_payments[token] = entry
            await m.reply(
                "⚠️ Payment received but activation failed.\n"
                f"Contact support with this token: `{token}`"
            )
            return

        try:
            if config.OWNER_ID:
                await c.send_message(
                    config.OWNER_ID,
                    f"Payment received!\nUser: `{uid}` | Plan: {entry['plan_label']} | "
                    f"{days}d | Rs.{entry['price']} | Link: `{entry['link_id']}`",
                )
        except Exception:
            logger.debug("Failed to notify owner of payment", exc_info=True)

        await m.reply(
            f"**Premium activated!**\n\n"
            f"Plan: **{entry['plan_label']}**\n"
            f"Valid until: `{fmt} IST`\n\n"
            f"Enjoy your premium features."
        )
        return

    return


@ratelimit("default")
async def help_cmd(c: Client, m: Message) -> None:
    """/help -- open the interactive Quizbot control centre."""
    uid = m.from_user.id if m.from_user else 0
    await m.reply(
        HELP_PAGES["home"],
        reply_markup=_help_keyboard("home", uid),
        disable_web_page_preview=True,
    )


@ratelimit("default")
async def features_cmd(c: Client, m: Message) -> None:
    """/features -- open the feature overview with menu navigation."""
    uid = m.from_user.id if m.from_user else 0
    await m.reply(
        HELP_PAGES["features"],
        reply_markup=_help_keyboard("features", uid),
        disable_web_page_preview=True,
    )


async def limit_cmd(c: Client, m: Message) -> None:
    """/limit -- show the caller's current rate-limit usage."""
    await m.reply(state.rate_limit_status_text(m.from_user.id))


def _is_owner_or_admin(user_id: int) -> bool:
    return user_id == config.OWNER_ID or user_id in config.ADMIN_IDS


async def gcast_cmd(c: Client, m: Message) -> None:
    """/gcast -- (owner/admin only) broadcast the replied-to message to
    every registered user."""
    uid = m.from_user.id
    if not _is_owner_or_admin(uid):
        return
    if not m.reply_to_message:
        await m.reply("Reply to the message you want to broadcast.")
        return
    if state.broadcast.active:
        await m.reply("A broadcast is already active. Use /stopcast to stop it.")
        return

    state.broadcast.active = True
    bm = m.reply_to_message
    users = await UserRepository(get_db()).get_all(limit=1_000_000)
    total, sent, failed = len(users), 0, 0
    progress = await m.reply(f"Starting broadcast: 0/{total}")

    for i, u in enumerate(users):
        if not state.broadcast.active:
            await progress.edit_text(f"Stopped at {sent}/{total}")
            return
        cid = u["chat_id"]
        try:
            if bm.text:
                await c.send_message(cid, bm.text, reply_markup=bm.reply_markup)
            elif bm.photo:
                await c.send_photo(cid, bm.photo.file_id, caption=bm.caption or "", reply_markup=bm.reply_markup)
            elif bm.video:
                await c.send_video(cid, bm.video.file_id, caption=bm.caption or "", reply_markup=bm.reply_markup)
            elif bm.document:
                await c.send_document(cid, bm.document.file_id, caption=bm.caption or "", reply_markup=bm.reply_markup)
            else:
                await bm.copy(cid)
            sent += 1
        except Exception:
            failed += 1
        if (i + 1) % 100 == 0:
            await progress.edit_text(f"Progress: {sent}/{total}")
            await asyncio.sleep(10)

    state.broadcast.active = False
    await progress.edit_text(f"Done: {sent}/{total} sent, {failed} failed")


async def stopcast_cmd(c: Client, m: Message) -> None:
    """/stopcast -- (owner/admin only) interrupt an active /gcast run."""
    if not _is_owner_or_admin(m.from_user.id):
        return
    if not state.broadcast.active:
        await m.reply("No active broadcast.")
        return
    state.broadcast.active = False
    await m.reply("Broadcast stopped.")


@ratelimit("default")
async def statses_cmd(c: Client, m: Message) -> None:
    """/statses -- overall platform stats (users, quizzes, paid/free)."""
    status = await m.reply("Fetching...")
    try:
        user_stats = await UserRepository(get_db()).stats()
        quiz_stats = await QuizRepository(get_db()).stats()
        await status.edit_text(
            "**Platform Stats**\n\n"
            f"Users: `{user_stats['total_users']}` (premium: `{user_stats['premium_users']}`)\n"
            f"Quizzes: `{quiz_stats['total_quizzes']}`\n"
            f"Paid: `{quiz_stats['paid_quizzes']}`\n"
            f"Free: `{quiz_stats['free_quizzes']}`"
        )
    except Exception as exc:
        logger.exception("statses_cmd failed")
        await status.edit_text(f"Error: {exc}")


async def testapi_cmd(c: Client, m: Message) -> None:
    """/testapi -- (owner only) sanity-check DB connectivity."""
    if m.from_user.id != config.OWNER_ID:
        return
    status = await m.reply("🔄 Testing database connectivity...")
    try:
        db = get_db()
        await db.db.command("ping")
        user_stats = await UserRepository(db).stats()
        quiz_stats = await QuizRepository(db).stats()
        await status.edit_text(
            "✅ **DB connectivity: OK**\n\n"
            f"Users: `{user_stats['total_users']}`\n"
            f"Quizzes: `{quiz_stats['total_quizzes']}`\n"
            f"DB: `MongoDB / {config.MONGODB_DB_NAME}`"
        )
    except Exception as exc:
        logger.exception("testapi_cmd failed")
        await status.edit_text(f"❌ DB connectivity FAILED: {exc}")


LEADERS_PAGE_SIZE = config.LEADERS_PAGE_SIZE


def _leaders_rich_md(quiz_name: str, rows: list[dict], start_rank: int) -> str:
    lines = [
        f"### 🥇 Leading Aspirants -- {quiz_name}",
        "",
        "| # | Name | Score | Qs | Time |",
        "|--:|:-----|------:|---:|-----:|",
    ]
    for j, r in enumerate(rows, start=start_rank):
        icon = "🥇" if j == 1 else "🥈" if j == 2 else "🥉" if j == 3 else str(j)
        name = str(r.get("user_name") or "Player")[:22].replace("|", "\\|")
        secs = r.get("time_taken", 0) or 0
        mn, sc = divmod(int(secs), 60)
        lines.append(f"| {icon} | {name} | {r.get('score', 0)} | {r.get('total_questions', 0)} | {mn}m {sc}s |")
    return "\n".join(lines)


async def _send_raw_bot_api(method: str, params: dict) -> dict:
    url = f"https://api.telegram.org/bot{config.CREATOR_BOT_TOKEN}/{method}"
    status, body = await request_json("POST", url, json_body=params, retries=1)
    if status != 200 or not isinstance(body, dict) or not body.get("ok"):
        raise RuntimeError(f"{method} failed: HTTP {status} {body}")
    return body


@ratelimit("default")
async def leaders_cmd(c: Client, m: Message) -> None:
    """/leaders <quiz_id> (or /aspirants) -- full ranked leaderboard for a quiz."""
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.reply("Usage: `/leaders QUIZID`")
        return
    qid = parts[1].strip()

    quiz = await QuizRepository(get_db()).get(qid)
    if not quiz:
        await m.reply("Quiz not found.")
        return

    board = LeaderboardRepository(get_db())
    rows = await board.page(qid, offset=0, limit=LEADERS_PAGE_SIZE)
    if not rows:
        await m.reply("No one has attempted this quiz yet.")
        return

    status = await m.reply(f"Found {len(rows)}+ aspirant(s). Sending leaderboard...")
    offset, rank = 0, 1
    page_rows = rows
    while page_rows:
        rich_md = _leaders_rich_md(quiz["quiz_name"], page_rows, rank)
        await send_rich_or_fallback(
            _send_raw_bot_api,
            lambda text: m.reply(text),
            m.chat.id,
            rich_md,
            plain_limit=3500,
        )
        rank += len(page_rows)
        offset += LEADERS_PAGE_SIZE
        page_rows = await board.page(qid, offset=offset, limit=LEADERS_PAGE_SIZE)
        if page_rows:
            await asyncio.sleep(1)

    try:
        await status.delete()
    except Exception:
        pass


def register(app: Client) -> None:
    app.on_message(filters.command("start") & filters.private)(start_cmd)
    app.on_message(filters.command("help"))(help_cmd)
    app.on_message(filters.command("features"))(features_cmd)
    app.on_callback_query(filters.regex(r"^hmenu:"))(help_menu_callback)
    app.on_message(filters.command("limit") & filters.private)(limit_cmd)
    app.on_message(filters.command("gcast") & filters.private)(gcast_cmd)
    app.on_message(filters.command("stopcast") & filters.private)(stopcast_cmd)
    app.on_message(filters.command("statses") & filters.private)(statses_cmd)
    app.on_message(filters.command("testapi") & filters.private)(testapi_cmd)
    app.on_message(filters.command(["leaders", "aspirants"]) & filters.private)(leaders_cmd)
