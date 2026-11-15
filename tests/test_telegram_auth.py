import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from quizbot.mini_app.telegram_auth import (
    MAX_INIT_DATA_AGE_SECONDS,
    verify_init_data,
)


BOT_TOKEN = "123456:unit-test-token"
USER = {"id": 42, "first_name": "Quiz", "last_name": "Tester", "username": "quiztester"}


def signed_init_data(fields: dict[str, str], token: str = BOT_TOKEN) -> str:
    """Build Telegram-style initData for tests (not a real Telegram token)."""
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def valid_fields(auth_date: int | None = None) -> dict[str, str]:
    return {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAE-test-query",
        "user": json.dumps(USER, separators=(",", ":")),
    }


def test_valid_init_data_returns_verified_user():
    session = verify_init_data(signed_init_data(valid_fields()), BOT_TOKEN)

    assert session is not None
    assert session.user.id == USER["id"]
    assert session.user.first_name == USER["first_name"]


def test_tampered_payload_is_rejected():
    payload = signed_init_data(valid_fields()).replace("AAE-test-query", "tampered")

    assert verify_init_data(payload, BOT_TOKEN) is None


def test_wrong_bot_token_is_rejected():
    assert verify_init_data(signed_init_data(valid_fields()), "different-token") is None


def test_stale_init_data_is_rejected():
    old_date = int(time.time()) - MAX_INIT_DATA_AGE_SECONDS - 1

    assert verify_init_data(signed_init_data(valid_fields(old_date)), BOT_TOKEN) is None


def test_far_future_auth_date_is_rejected():
    future_date = int(time.time()) + 120

    assert verify_init_data(signed_init_data(valid_fields(future_date)), BOT_TOKEN) is None


def test_duplicate_query_parameter_is_rejected_even_if_signed():
    fields = valid_fields()
    fields["query_id"] = "second-value"
    signature = signed_init_data(fields).split("hash=", 1)[1]
    raw = urlencode({key: value for key, value in fields.items() if key != "query_id"})
    raw += "&query_id=first-value&query_id=second-value&hash=" + signature

    assert verify_init_data(raw, BOT_TOKEN) is None


def test_oversized_init_data_is_rejected():
    assert verify_init_data("x" * 20_000, BOT_TOKEN) is None


def test_empty_credentials_are_rejected():
    assert verify_init_data("", BOT_TOKEN) is None
    assert verify_init_data("some-data", "") is None
