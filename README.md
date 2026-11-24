<div align="center">

# 🎯 Quizbot

**A production-grade Telegram quiz platform — create, manage, and run interactive quizzes at scale.**

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![PTB](https://img.shields.io/badge/python--telegram--bot-22.8-0088CC?logo=telegram)](https://python-telegram-bot.org)
[![MongoDB](https://img.shields.io/badge/MongoDB-Atlas-47A248?logo=mongodb)](https://mongodb.com)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](https://docker.com)

---

**Originally developed by [devgagan](https://github.com/devgaganin) &nbsp;•&nbsp; Sponsored by [Qzio](https://qzio.in)**

---

### 🤖 Try the Live Bot → [@advance_quiz_bot](https://t.me/advance_quiz_bot)

</div>

---

## 📋 Table of Contents

- [Features](#-features)
- [Architecture](#-architecture)
- [Prerequisites](#-prerequisites)
- [Quick Start](#-quick-start)
- [Configuration Reference](#-configuration-reference)
- [Running the Platform](#-running-the-platform)
  - [Run the Unified Bot](#run-the-unified-bot)
  - [Mini App Only](#mini-app-only)
  - [systemd (VPS, always-on)](#systemd-vps-always-on)
- [Docker Deployment](#-docker-deployment)
- [Mini App — the Visual Quiz Player](#-mini-app--the-visual-quiz-player)
- [Database](#-database)
- [Credits](#-credits)

---

## ✨ Features

| Category | Capability |
|---|---|
| **Quiz Creation** | Text input, forwarded Telegram quiz polls, TXT/JSON/PDF/DOCX import, source-grounded passage-to-MCQ generation, AI-generated quizzes |
| **Quiz Formats** | Standard, sectional (per-section timers), practice & exam modes |
| **Smart Filtering** | Strips `[1/100]`-style progress tags, usernames, links, and custom word lists from imported polls |
| **Editing** | Shuffle questions, retitle, adjust timers, add/remove questions |
| **Access Control** | Free and paid quiz tiers, batch access, auth-chat lists, optional premium gate |
| **Analytics** | Per-user performance, leaderboards, sectional score breakdowns |
| **Timed Poll Quizzes** | Auto-closing Telegram quiz polls, optional anonymous mode, question/option shuffling, final group leaderboard |
| **Poll Preferences** | Per-user saved timer, anonymity, shuffle and leaderboard settings |
| **Scheduling** | MongoDB-backed pending schedules restored automatically after restarts |
| **HTML Reports** | Self-contained interactive HTML scorecards — question navigator, KaTeX/Markdown rendering, dark/light theme |
| **Mini App** | Visual in-Telegram quiz player (practice + exam mode) as a Telegram WebApp |
| **Inline Sharing** | Share any quiz by ID via inline query, with a working Play button |
| **Payments** | Razorpay-backed premium plans |
| **Broadcast** | Send announcements to all users (owner only) |

---

## 🏗 Architecture

```
quizbot/
├── database/            Shared async MongoDB layer and repositories
├── shared/              Configuration, parsers, timers, utilities and HTML reports
├── runner_bot/          The single python-telegram-bot Bot API application
│   ├── bot.py            Application setup and polling lifecycle
│   └── handlers/         Quiz player, creator, editor, batches, reports, polls, payments
├── creator_bot/         Shared legacy parsing/state/payment utilities; MTProto transport is inactive
└── mini_app/            Optional FastAPI visual quiz player

run.py                  Single-client launcher (+ optional Mini App)
requirements.txt
Procfile / Dockerfile / docker-compose.yml
.env.example             Environment variable template
```

The live deployment uses **one Telegram Bot API connection and one BotFather token**. The same application handles quiz creation, editing, imports, batch management, paid access, AI keys, reports, scheduling, timed polls, quiz playback and leaderboards. The old `creator_bot/bot.py` and Pyrogram handler transport are not started by the live launcher. Some transport-independent utilities (question parsing, shared state, source-question logic and payment helpers) are reused by the Bot API handlers. `API_ID` and `API_HASH` are not required.

The optional Mini App runs alongside the bot when `MINI_APP_DOMAIN` is configured. Both components use the same MongoDB database; quiz IDs and existing stored quizzes are preserved.

---

## 🔧 Prerequisites

| Requirement | Minimum Version | Notes |
|---|---|---|
| Python | 3.11+ | |
| MongoDB | Atlas free tier (M0) or self-hosted | `MONGODB_URI` in `.env` |
| Telegram bot token | BotFather | One token from [@BotFather](https://t.me/BotFather) for all bot commands |

---

## ⚡ Quick Start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env          # fill in your values — see Configuration Reference below

python run.py
```

The database and all its indexes are created automatically on first connect — no manual schema step needed.

### Source-to-quiz and timed polls

Use `/sourcequiz 10` (or `/createfromtext 10`) in the bot's private chat, then send a passage or upload a `.txt`, `.md`, `.csv`, `.pdf`, `.docx` or `.json` file. Exit with `/sourcequizcancel`. Formatted MCQs are parsed locally; passages use the shared AI-provider key system with strict source-evidence checks, followed by a conservative offline fact extractor if generation is unavailable. The resulting quiz is saved in the normal MongoDB quiz library and can be launched with either `/start QUIZ_ID` or `/pollquiz QUIZ_ID`.

Use `/pollsettings` to save your default timed-poll duration, anonymity, question/option shuffling and leaderboard preference. An explicit `/pollquiz QUIZ_ID 30 public` setting overrides your default timer/anonymity for that run. Pending schedules are stored in MongoDB and restored after a restart. Use `/schedule QUIZ_ID 14:30` for the existing regular quiz scheduler, or `/schedule QUIZ_ID 15m` (also `2h` / `1d`) to publish a timed poll quiz later with your saved poll preferences. `/viewschedule` and `/myschedules` list both types; `/cancelschedule` accepts a schedule ID or quiz ID.

---

## ⚙️ Configuration Reference

All values live in `.env` (copy from [`.env.example`](.env.example)). Note: you must not expose these values in the repo you fork directly otherwise you may loose your bot, data (securely fill these vars and secret in the environment of the platform you are using) 

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | ✅ | One BotFather token for all creator and quiz-player commands |
| `API_ID` / `API_HASH` | — | Not required by the unified Bot API runtime; old values may remain in Render |
| `CREATOR_BOT_TOKEN` / `RUNNER_BOT_TOKEN` | — | Legacy names only; set `BOT_TOKEN` for the unified deployment |
| `MONGODB_URI` | ✅ | MongoDB Atlas connection string |
| `MONGODB_DB_NAME` | ✅ | Database name (default: `quizbot`) |
| `OWNER_ID` | ✅ | Your Telegram user ID |
| `ADMIN_IDS` | ➖ | Space-separated additional admin user IDs |
| `LOG_GROUP` | ➖ | Negative chat ID for error/log channel |
| `BOT_GROUP` | ➖ | Main community group ID |
| `CHANNEL_ID` | ➖ | Announcement channel ID |
| `REQUIRED_SUB_CHANNEL` | ➖ | Channel users must join to use `/start`, `/create`, `/myquizzes`, `/add` |
| `FREE_BOT` | ➖ | `true` to treat every user as premium |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | ➖ | Leave blank to disable the `/pay` premium-purchase flow |
| `PDF_API_BASE` | ➖ | Optional external PDF-generation microservice for `/testseries` |
| `MINI_APP_DOMAIN` | ➖ | Public HTTPS URL for the Mini App — leave blank to disable it entirely |
| `MINI_APP_HOST` / `MINI_APP_PORT` | ➖ | Local bind address behind your reverse proxy (default `0.0.0.0:8080`) |
| `OPENROUTER_DEFAULT_KEYS` | ➖ | Comma-separated fallback AI provider keys |

Rate limits, session timeouts, and other tuning knobs have sensible defaults — see the comments in `.env.example` for the full list.

---

## 🚀 Running the Platform

### Run the Unified Bot

```bash
python run.py
```

Starts the single Bot API application, and the optional Mini App server too when `MINI_APP_DOMAIN` is set. There is no `creator` or `runner` second-bot process.

### Mini App Only

```bash
python run.py --only miniapp
```

This starts only the FastAPI Mini App. The normal Render, Docker and VPS deployment should use `python run.py`.

### systemd (VPS, always-on)

```bash
sudo nano /etc/systemd/system/quizbot.service
```

```ini
[Unit]
Description=Quizbot Platform
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/quizbot
ExecStart=/opt/quizbot/.venv/bin/python run.py
Restart=always
RestartSec=10
EnvironmentFile=/opt/quizbot/.env
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now quizbot
sudo journalctl -u quizbot -f      # live logs
```

---

## 🐳 Docker Deployment

```bash
docker compose up -d --build
```

This starts the unified Bot API service (and the optional Mini App in the same process when configured), connecting to the same MongoDB Atlas cluster via `MONGODB_URI`. No MTProto session, Render Shell, or second bot service is required.

**Heroku / other PaaS**: a single `Procfile` (`web: python run.py`) runs the whole platform from one dyno/process — set the same `.env` variables as Config Vars.

---

## 📱 Mini App — the Visual Quiz Player

A "Play" button (opened as a Telegram WebApp) appears after quiz creation and on inline-share cards, offering two modes:

- **Practice mode** — instant correct/incorrect feedback with the explanation shown right after each answer, then auto-advance.
- **Exam mode** — no answers revealed until the end, followed by a full top-to-bottom review of every question, your answer, the correct answer, and the explanation.

It's strictly a player — no creation or editing happens here, and it enforces the same access rules as the unified bot (free/paid quizzes, batch access, auth-chat lists, optional premium gate).

Telegram requires a public HTTPS URL for WebApp buttons, so put a reverse proxy or tunnel (nginx, Caddy, Cloudflare Tunnel, etc.) in front of the FastAPI server and set `MINI_APP_DOMAIN` accordingly. Leave it blank to disable the feature entirely — no Play buttons are shown, and the server doesn't start.

Identity comes solely from Telegram's own `initData`, verified server-side via HMAC-SHA256 on every request. Quiz content in every API response is AES-256-GCM encrypted with a per-session key, and the correct answer is never present in a question's payload before it's answered.

---

## 🗄 Database

Data lives in MongoDB Atlas — a free M0 cluster is enough to get started (see [Quick Start](#-quick-start)). `quizbot/database/db.py` connects via PyMongo's native async API and creates every required index automatically on first connect, so there's no manual schema step. All Bot API handlers read and write through repository classes in `quizbot/database/repositories.py` — there is no second creator process or duplicate polling connection to keep in sync.

---

## 🙏 Credits

<div align="center">

| Role | |
|---|---|
| **Originally developed by** | [devgagan](https://github.com/devgaganin) |
| **Sponsored by** | [Qzio](https://qzio.in) — The Smart Quiz Platform |
| **Telegram Library** | [python-telegram-bot](https://python-telegram-bot.org) — Bot API |
| **Database** | [MongoDB Atlas](https://mongodb.com) |

---

*Built for educators, exam aspirants, and quiz creators.*

</div>
