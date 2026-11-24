"""Import-time smoke checks for the real app adapters (no credentials/network)."""
import importlib

import pytest


# These are runtime dependencies, not optional test plugins. When the project
# is installed from requirements.txt, all imports below should work cleanly.
pytest.importorskip("telegram")
pytest.importorskip("pymongo")


@pytest.mark.parametrize(
    "module_name",
    [
        "quizbot.database.db",
        "quizbot.database.repositories",
        "quizbot.runner_bot.bot",
        "quizbot.runner_bot.handlers",
        "quizbot.runner_bot.handlers.creator",
        "quizbot.runner_bot.handlers.creator_batches",
        "quizbot.runner_bot.handlers.creator_reports",
        "quizbot.runner_bot.handlers.creator_admin",
        "quizbot.shared.quiz_file_import",
        "quizbot.mini_app.routes",
        "run",
    ],
)
def test_application_modules_import_without_side_effects(module_name):
    assert importlib.import_module(module_name) is not None
