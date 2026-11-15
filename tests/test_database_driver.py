import asyncio
import inspect

import pytest


def test_pymongo_async_driver_is_installed_and_selected():
    pymongo = pytest.importorskip("pymongo")
    from pymongo import AsyncMongoClient
    from pymongo.asynchronous.collection import AsyncCollection
    from pymongo.asynchronous.database import AsyncDatabase

    from quizbot.database import db as database_module

    assert database_module.AsyncMongoClient is AsyncMongoClient
    assert database_module.AsyncCollection is AsyncCollection
    assert database_module.AsyncDatabase is AsyncDatabase
    assert inspect.iscoroutinefunction(AsyncMongoClient.close)


def test_database_close_awaits_client_shutdown():
    pytest.importorskip("pymongo")
    from quizbot.database.db import Database

    class FakeAsyncClient:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    database = Database.__new__(Database)
    database._client = FakeAsyncClient()
    database._db = object()
    client = database._client

    asyncio.run(database.close())

    assert client.closed is True
    assert database._client is None
    assert database._db is None
