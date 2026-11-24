"""Regression tests for transient Telegram MTProto startup failures."""

import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BOT_PATH = ROOT / "quizbot" / "creator_bot" / "bot.py"


def _load_startup_helpers():
    """Load the helper functions without requiring a live Pyrogram install."""
    original = sys.modules.get("pyrogram")
    stub = types.ModuleType("pyrogram")
    stub.Client = object
    sys.modules["pyrogram"] = stub
    try:
        spec = importlib.util.spec_from_file_location("_creator_bot_retry_test", BOT_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        if original is None:
            sys.modules.pop("pyrogram", None)
        else:
            sys.modules["pyrogram"] = original


_helpers = _load_startup_helpers()


class FakeRpcError(Exception):
    CODE = 500


class FakeAuthError(Exception):
    CODE = 401


def test_timeout_is_treated_as_transient():
    assert _helpers._is_transient_start_error(TimeoutError("authorization timed out"))


def test_network_error_is_treated_as_transient():
    assert _helpers._is_transient_start_error(ConnectionError("connection reset"))


def test_telegram_server_error_is_treated_as_transient():
    assert _helpers._is_transient_start_error(FakeRpcError("RPC_CALL_FAIL"))


def test_authentication_error_is_not_silently_retried():
    assert not _helpers._is_transient_start_error(FakeAuthError("UNAUTHORIZED"))


def test_retry_delay_uses_capped_exponential_backoff():
    assert [_helpers._retry_delay(i) for i in range(1, 7)] == [5, 10, 20, 40, 60, 60]
