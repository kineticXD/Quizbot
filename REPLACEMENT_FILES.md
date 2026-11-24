# Quizbot Main upgrade — timed poll studio integration

This patch merges the useful poll-quiz workflows from `poll-quiz-bot` into the existing Quizbot Main architecture. It is a **replacement-file patch**, not a second bot: generated quizzes use Quizbot's existing quiz repository, and the runner uses the existing timed-poll engine.

## Install

1. Back up your current Quizbot Main project and its `.env` file.
2. Extract this ZIP into the **root folder of Quizbot Main**, keeping the folder structure. Allow the listed files to overwrite the old versions and add the new files.
3. From that project folder, install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Keep your existing `.env` values and MongoDB database. No new Telegram bot token or separate database is required.
5. Restart both bot processes using your normal deployment method.
6. Update the commands in BotFather using `BOTFATHER_COMMANDS.txt` if you want the new commands to appear in Telegram's command menu.

The runner automatically creates the MongoDB indexes for `poll_quiz_settings` and `scheduled_quizzes`. Pending schedules are restored when the runner starts. Keep the same MongoDB database between restarts.

## New creator workflow: create a quiz from source material

In the creator bot's private chat:

- `/sourcequiz 10` — begin creating up to 10 questions from a passage or uploaded file.
- `/createfromtext 10` — alias.
- Send/paste the passage, or upload `.txt`, `.md`, `.csv`, `.pdf`, `.docx`, or `.json` material.
- `/sourcequizcancel` — cancel the current source-to-quiz flow.

Already formatted MCQs are parsed locally. For plain passages, the feature reuses the creator bot's configured AI provider/key system and performs source-evidence validation; a conservative local extractor is used as a fallback where possible. The result is saved in the usual MongoDB quiz library and gets a normal Quiz ID. Use that same ID with `/start QUIZ_ID` or `/pollquiz QUIZ_ID`.

## Saved timed-poll preferences

In the runner bot:

- `/pollsettings` — set the default poll auto-close timer, anonymity, question shuffle, option shuffle and final leaderboard preference.
- `/pollquiz QUIZ_ID [minutes] [anonymous|public]` — publish a quiz as timed Telegram polls.
- `/quizpoll` and `/publish` — aliases for `/pollquiz`.
- `/pollstop` — stop an active poll quiz.

An explicit timer/mode on `/pollquiz` overrides the saved default for that run. Telegram channel polls are anonymous by platform design; personal score tracking is therefore not available for channel polls.

## Scheduling: existing mode plus new timed-poll mode

The original schedule syntax is preserved:

- `/schedule QUIZ_ID 14:30` — schedule the regular quiz runner for that time (IST).

New relative-delay syntax schedules a timed poll quiz:

- `/schedule QUIZ_ID 15m`
- `/schedule QUIZ_ID 2h`
- `/schedule QUIZ_ID 1d`

Relative delays are accepted from 1 minute up to 30 days. The timed-poll schedule captures the creator's saved `/pollsettings` at scheduling time. Use `/viewschedule` or `/myschedules` to view pending schedules, and `/cancelschedule SCHEDULE_ID` (or the quiz ID) to cancel one.

## Replacement manifest

This patch contains 20 files: 6 new files and 14 modified files. No original files are deleted.

### New files

- `quizbot/creator_bot/handlers/source_quiz.py`
- `quizbot/creator_bot/source_quiz_engine.py`
- `quizbot/runner_bot/handlers/poll_settings.py`
- `quizbot/runner_bot/schedule_utils.py`
- `tests/test_scheduling.py`
- `tests/test_source_quiz_engine.py`

### Modified files

- `BOTFATHER_COMMANDS.txt`
- `README.md`
- `requirements.txt`
- `quizbot/creator_bot/handlers/__init__.py`
- `quizbot/creator_bot/handlers/admin.py`
- `quizbot/creator_bot/handlers/quiz_creation.py`
- `quizbot/database/__init__.py`
- `quizbot/database/db.py`
- `quizbot/database/repositories.py`
- `quizbot/runner_bot/bot.py`
- `quizbot/runner_bot/handlers/__init__.py`
- `quizbot/runner_bot/handlers/admin.py`
- `quizbot/runner_bot/handlers/poll_quiz.py`
- `quizbot/runner_bot/handlers/scheduling.py`

## Validation and limitations

The source compiles, and the automated test suite reports **100 passed, 6 skipped**. The skipped tests require optional runtime packages that are not installed in the build environment (Pyrogram, python-telegram-bot and/or PyMongo). This environment did not have the live Telegram credentials or MongoDB connection needed for an end-to-end deployment test, so run a private-chat and test-group smoke test after installing dependencies and configuring your existing credentials.

The upgrade does not start or operate a second poll bot. It adds the missing poll-bot workflows to Quizbot Main's existing creator, runner and database layers.
