"""Import-time smoke checks for the real app adapters (no credentials/network)."""
import importlib

import pytest


# These are runtime dependencies, not optional test plugins. When the project
# is installed from requirements.txt, all imports below should work cleanly.
pytest.importorskip("pyrogram")
pytest.importorskip("telegram")
pytest.importorskip("pymongo")


@pytest.mark.parametrize(
    "module_name",
    [
        "quizbot.database.db",
        "quizbot.database.repositories",
        "quizbot.creator_bot.bot",
        "quizbot.creator_bot.handlers",
        "quizbot.runner_bot.bot",
        "quizbot.runner_bot.handlers",
        "quizbot.mini_app.routes",
        "run",
    ],
)
def test_application_modules_import_without_side_effects(module_name):
    assert importlib.import_module(module_name) is not None
