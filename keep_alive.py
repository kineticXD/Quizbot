"""Small optional health endpoint for hosts that require an HTTP service.

When the Mini App is enabled, its FastAPI server owns the host-provided PORT
and acts as the health endpoint too. This module is only started when the Mini
App is disabled, preventing the old Flask/FastAPI port collision.
"""

from __future__ import annotations

import logging
import os
from threading import Thread

from flask import Flask

logger = logging.getLogger(__name__)
app = Flask("quizbot-health")


@app.get("/")
def home():
    return {"status": "ok", "service": "quizbot"}


@app.get("/health")
def health():
    return {"status": "healthy"}


def keep_alive() -> Thread:
    """Start the health endpoint once in a daemon thread."""
    port = int(os.environ.get("PORT") or os.environ.get("MINI_APP_PORT", "8080"))
    thread = Thread(
        target=lambda: app.run(
            host="0.0.0.0", port=port, use_reloader=False, threaded=True
        ),
        name="quizbot-health",
        daemon=True,
    )
    thread.start()
    logger.info("Health endpoint starting on 0.0.0.0:%s", port)
    return thread
