import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def premium_module(monkeypatch):
    """Load premium.py in isolation so these policy tests need no Mongo driver."""
    fake_database = types.ModuleType("quizbot.database")
    fake_database.UserRepository = object
    fake_database.get_db = lambda: None
    monkeypatch.setitem(sys.modules, "quizbot.database", fake_database)

    path = Path(__file__).parents[1] / "quizbot" / "shared" / "utils" / "premium.py"
    spec = importlib.util.spec_from_file_location("premium_access_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeUsers:
    def __init__(self, _db, result=False):
        self.result = result
        self.checked = []

    async def is_premium(self, user_id):
        self.checked.append(user_id)
        return self.result


@pytest.mark.parametrize(
    ("user_id", "owner_id", "admin_ids"),
    [
        (123456, 123456, []),
        (987654, 123456, [987654]),
    ],
)
def test_owner_and_admin_bypass_database_premium_gate(
    premium_module, monkeypatch, user_id, owner_id, admin_ids
):
    premium = premium_module
    monkeypatch.setattr(premium.config, "FREE_BOT", False)
    monkeypatch.setattr(premium.config, "OWNER_ID", owner_id)
    monkeypatch.setattr(premium.config, "ADMIN_IDS", admin_ids)

    def unexpected_db_access():
        raise AssertionError("Owner/admin bypass should not query MongoDB")

    monkeypatch.setattr(premium, "get_db", unexpected_db_access)
    assert asyncio.run(premium.is_premium_user(user_id)) is True


def test_regular_user_still_uses_subscription_record(premium_module, monkeypatch):
    premium = premium_module
    monkeypatch.setattr(premium.config, "FREE_BOT", False)
    monkeypatch.setattr(premium.config, "OWNER_ID", 123456)
    monkeypatch.setattr(premium.config, "ADMIN_IDS", [])
    fake_repo = _FakeUsers(object(), result=True)
    monkeypatch.setattr(premium, "get_db", lambda: object())
    monkeypatch.setattr(premium, "UserRepository", lambda db: fake_repo)

    assert asyncio.run(premium.is_premium_user(555555)) is True
    assert fake_repo.checked == [555555]


def test_free_bot_flag_still_bypasses_database(premium_module, monkeypatch):
    premium = premium_module
    monkeypatch.setattr(premium.config, "FREE_BOT", True)
    monkeypatch.setattr(premium.config, "OWNER_ID", None)
    monkeypatch.setattr(premium.config, "ADMIN_IDS", [])

    def unexpected_db_access():
        raise AssertionError("FREE_BOT should not query MongoDB")

    monkeypatch.setattr(premium, "get_db", unexpected_db_access)
    assert asyncio.run(premium.is_premium_user(555555)) is True
