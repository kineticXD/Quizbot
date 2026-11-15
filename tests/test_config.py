import importlib

import pytest

import quizbot.shared.config as config


_ENV_KEYS = (
    "BOT_TOKEN", "CREATOR_BOT_TOKEN", "RUNNER_BOT_TOKEN",
    "API_ID", "API_HASH", "MONGODB_URI", "MONGODB_DB_NAME", "OWNER_ID",
)


@pytest.fixture

def clean_config(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    loaded = importlib.reload(config)
    yield loaded
    importlib.reload(config)


def set_base_environment(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017/test")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.setenv("API_ID", "12345")
    monkeypatch.setenv("API_HASH", "test-api-hash")


def test_one_bot_token_configures_both_internal_adapters(clean_config, monkeypatch):
    set_base_environment(monkeypatch)
    monkeypatch.setenv("BOT_TOKEN", "123:single-token")

    loaded = importlib.reload(config)

    assert loaded.CREATOR_BOT_TOKEN == "123:single-token"
    assert loaded.RUNNER_BOT_TOKEN == "123:single-token"
    assert loaded.BOT_TOKENS == ("123:single-token",)
    assert loaded.validate("both") == []


def test_single_legacy_token_is_accepted_for_migration(clean_config, monkeypatch):
    set_base_environment(monkeypatch)
    monkeypatch.setenv("CREATOR_BOT_TOKEN", "123:legacy-token")

    loaded = importlib.reload(config)

    assert loaded.CREATOR_BOT_TOKEN == "123:legacy-token"
    assert loaded.RUNNER_BOT_TOKEN == "123:legacy-token"
    assert loaded.BOT_TOKENS == ("123:legacy-token",)


def test_distinct_legacy_tokens_remain_available_for_existing_deployments(clean_config, monkeypatch):
    set_base_environment(monkeypatch)
    monkeypatch.setenv("CREATOR_BOT_TOKEN", "123:creator-legacy")
    monkeypatch.setenv("RUNNER_BOT_TOKEN", "456:runner-legacy")

    loaded = importlib.reload(config)

    assert loaded.CREATOR_BOT_TOKEN == "123:creator-legacy"
    assert loaded.RUNNER_BOT_TOKEN == "456:runner-legacy"
    assert loaded.BOT_TOKENS == ("123:creator-legacy", "456:runner-legacy")


def test_runner_does_not_require_mtproto_api_credentials(clean_config, monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017/test")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.setenv("BOT_TOKEN", "123:single-token")

    loaded = importlib.reload(config)

    assert loaded.validate("runner") == []
    assert any("API_ID / API_HASH" in item for item in loaded.validate("creator"))


def test_mini_app_only_requires_database_and_bot_token(clean_config, monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017/test")
    monkeypatch.setenv("BOT_TOKEN", "123:single-token")

    loaded = importlib.reload(config)

    assert loaded.validate("miniapp") == []
