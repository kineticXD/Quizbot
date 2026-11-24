SHIKIMORI / QUIZBOT — SINGLE BOT API MIGRATION
================================================

WHAT THIS PATCH DOES
--------------------
Moves the live quiz creator and quiz player onto one python-telegram-bot
(Bot API) application using the same BotFather token. The old Pyrogram /
Kurigram creator transport is no longer started by run.py, so a failure in
Telegram's MTProto bot authorization cannot disable creator commands or
crash the service.

The existing MongoDB database and quiz schema are retained. Existing Quiz
IDs are not rewritten, and quizzes created through the migrated creator use
the same repository/schema as /start and /pollquiz.

MAIN MIGRATED WORKFLOWS
-----------------------
- Quiz creation, question entry, forwarded Telegram quiz polls, TXT/JSON imports
- Section-wise quiz setup, per-section timers, promo text and saved quick-save defaults
- Attach newly-created paid quizzes to existing or new batches from the result message
- Quiz library, search, deletion, info and inline sharing
- Quiz editing: title, timer, type, negative marks, question add/delete, promo,
  question/option shuffle
- Source-to-quiz from text, TXT, MD, CSV, PDF, DOCX and JSON
- AI provider key management and creator word filters/settings
- Paid-chat permissions, premium payment flow and admin premium controls
- Batch creation, editing, quiz/chat attachment and search
- Creator HTML/test-series reports and owner/admin utilities
- Existing quiz playback, timer controls, timed poll quizzes, saved poll settings,
  scheduling, leaderboards and reports

INSTALLATION ON GITHUB + RENDER FREE
------------------------------------
1. Download this ZIP and extract it into the ROOT of your existing upgraded
   Quizbot Main / Shikimori GitHub repository. Preserve the folder structure
   and allow the listed files to be replaced.
2. Do NOT upload your .env file and do NOT paste secrets into GitHub.
3. Keep the existing Render environment variables. The only required Telegram
   credential for the unified bot is BOT_TOKEN. API_ID and API_HASH may remain
   in Render but are no longer required by the live runtime.
4. Keep the same MONGODB_URI, MONGODB_DB_NAME and OWNER_ID. No database reset
   or migration is required.
5. Commit and push the replacement files to GitHub. Render should redeploy
   from the connected repository automatically.
6. The normal Render Start Command should be: python run.py
   The standard Build Command is: pip install -r requirements.txt

Do not run a second creator service alongside this unified process. The one
Bot API application must be the only polling instance for this bot token.

EXPECTED STARTUP LOGS
---------------------
- Starting unified quiz and creator features on the Bot API account
- Unified Bot API polling started.

You should no longer see creator_adapter startup or auth.ImportBotAuthorization
errors from this application, because the MTProto transport is not loaded.

POST-DEPLOYMENT SMOKE TEST
--------------------------
In a private chat with the bot:
1. /start
2. /help
3. /creatorstatus
4. /create, send a quiz title, then add at least 10 parsed questions or import
   forwarded quiz polls / supported TXT or JSON files.
5. /done and complete the free/paid + setup prompts.
6. Confirm the returned Quiz ID works with /start QUIZ_ID.
7. Test /pollquiz QUIZ_ID in a test group or channel where the bot has the
   permissions needed to send and stop polls.
8. Test /myquizzes, /edit QUIZ_ID, /sourcequiz 10 and /batch.

LIMITATIONS / HONEST TEST STATUS
--------------------------------
The source was syntax-compiled and the automated local suite passed. This
workspace did not have the pinned Telegram Bot API and MongoDB runtime packages
installed, and network access prevented installing them here. Six runtime
import tests are therefore skipped. No live Render deployment, Telegram group
permissions, MongoDB Atlas operation or Razorpay payment was executed from this
workspace. Verify those in the deployed service using the smoke test above.

If the Bot API token is also invalid or the Bot API itself is unavailable, this
migration cannot make Telegram accept it. It specifically removes the separate
MTProto authorization dependency that caused the failures in the provided
logs.
