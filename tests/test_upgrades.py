"""Tests for Quizbot upgrades: AI providers, Mini App security/TTL, repositories, and wiring."""
from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_config_gemini_model_and_provider_urls():
    from quizbot.shared import config

    assert "gemini-2.5-flash" in config.GEMINI_URL
    assert config.OPENAI_URL.startswith("https://")
    assert config.MISTRAL_URL.startswith("https://")
    assert not hasattr(config, "POLLQUIZ_AUTO_CLOSE_SECONDS")


def test_parse_ai_questions_handles_trailing_brackets():
    # Stub pymongo/motor-free import path for ai_providers if needed
    from quizbot.runner_bot.ai_providers import parse_ai_questions

    raw = (
        '```json\n'
        '[{"q":"What is 2+2?","o":["3","4","5","6"],"c":[1],"e":"Basic addition"}]\n'
        '```\n'
        'Note: [verified by AI]'
    )
    qs = parse_ai_questions(raw)
    assert len(qs) == 1
    assert qs[0]["question"] == "What is 2+2?"
    assert qs[0]["correct_option_id"] == 1


def test_ai_providers_try_call_supports_openai_mistral_and_pollinations():
    from quizbot.runner_bot import ai_providers

    async def _run():
        with patch.object(ai_providers, "_call_openai", new=AsyncMock(return_value="openai_ok")) as m_oai, \
             patch.object(ai_providers, "_call_mistral", new=AsyncMock(return_value="mistral_ok")) as m_mis, \
             patch.object(ai_providers, "_call_pollinations", new=AsyncMock(return_value="poll_ok")) as m_pol:
            assert await ai_providers._try_call("openai", "k1", "p", 100) == "openai_ok"
            assert await ai_providers._try_call("mistral", "k2", "p", 100) == "mistral_ok"
            assert await ai_providers._try_call("pollinations", "", "p", 100) == "poll_ok"
            m_oai.assert_awaited_once()
            m_mis.assert_awaited_once()
            m_pol.assert_awaited_once()

    asyncio.run(_run())


def test_mini_app_session_ttl_eviction_and_sent_at_idempotency():
    # Stub heavy optional deps if missing so player_service imports cleanly
    from quizbot.mini_app import player_service

    player_service._sessions.clear()
    now = time.time()
    player_service._sessions["old_1"] = {"created_at": now - 10000}
    player_service._sessions["fresh_1"] = {
        "created_at": now - 10,
        "order": [0],
        "quiz": {"questions": [{"question": "Q1", "options": ["A", "B"], "correct_option_id": 0}]},
        "per_question": {0: {"options": ["A", "B"], "correct_ids": [0]}},
        "sent_at": {},
    }

    evicted = player_service.cleanup_expired_sessions(max_age_seconds=3600)
    assert evicted == 1
    assert "old_1" not in player_service._sessions
    assert "fresh_1" in player_service._sessions

    q1 = player_service.public_question(player_service._sessions["fresh_1"], 0)
    assert q1 is not None
    first_ts = player_service._sessions["fresh_1"]["sent_at"][0]
    time.sleep(0.02)
    player_service.public_question(player_service._sessions["fresh_1"], 0)
    assert player_service._sessions["fresh_1"]["sent_at"][0] == first_ts
    player_service._sessions.clear()


def test_routes_exam_mode_redacts_answers():
    source = (ROOT / "quizbot/mini_app/routes.py").read_text(encoding="utf-8")
    assert 'if session.get("mode") == "exam":' in source
    assert '{"recorded": True}' in source


def test_player_service_preserves_fractional_scores():
    source = (ROOT / "quizbot/mini_app/player_service.py").read_text(encoding="utf-8")
    assert "final_score = round(total_score, 2)" in source


def test_index_html_has_review_filters_and_keyboard_shortcuts():
    html = (ROOT / "quizbot/mini_app/static/index.html").read_text(encoding="utf-8")
    assert "review-filters" in html
    assert 'document.addEventListener("keydown"' in html


def test_mystats_and_mistakes_commands_registered_and_documented():
    qp = (ROOT / "quizbot/runner_bot/handlers/quiz_play.py").read_text(encoding="utf-8")
    admin = (ROOT / "quizbot/runner_bot/handlers/admin.py").read_text(encoding="utf-8")
    bf = (ROOT / "BOTFATHER_COMMANDS.txt").read_text(encoding="utf-8")
    assert 'CommandHandler("mystats", mystats_command)' in qp
    assert 'CommandHandler("mistakes", mistakes_command)' in qp
    assert "/mystats" in admin
    assert "/mistakes" in admin
    assert "mystats -" in bf
    assert "mistakes -" in bf


def test_quiz_play_releases_session_and_replaces_stale_setup():
    qp = (ROOT / "quizbot/runner_bot/handlers/quiz_play.py").read_text(encoding="utf-8")
    assert "pending_quiz_settings.pop(chat.id, None)" in qp
    assert 'await session_mgr.delete(chat_id)\n        await safe_send_message(ctx, chat_id, f"❌ Quiz error: {e}")' in qp
    assert 'logger.warning("Persistence failed for quiz in chat=%s; releasing session", chat_id)' in qp
