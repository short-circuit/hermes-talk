"""Shared pytest fixtures for the relay test suite."""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from relay.app import create_app
from relay.config import Settings, reset_settings_cache
from relay.logging import reset_audit, reset_redaction, reset_registered_secrets

_ENV_KEYS = (
    "RELAY_HOST",
    "RELAY_PORT",
    "HERMES_BASE_URL",
    "HERMES_API_KEY",
    "HERMES_SESSION_KEY",
    "LOCALAI_BASE_URL",
    "VOICE_BRAIN_MODEL",
    "STT_MODEL",
    "TTS_MODEL",
    "VAD_MODEL",
    "DEVICE_TOKENS",
    "INBOX_DIR",
    "OUTBOX_DIR",
    "DATA_DIR",
    "TOKEN_STATE_PATH",
    "MAX_ATTACHMENT_MB",
    "MAX_CALL_MINUTES",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "LOG_TRANSCRIPT_MAX_CHARS",
    "LOG_MAX_AGE_DAYS",
    "LOG_MAX_BYTES",
    "LOG_BACKUP_COUNT",
    "AUDIT_MAX_AGE_DAYS",
    "AUDIT_MAX_BYTES",
    "AUDIT_BACKUP_COUNT",
    "RECORD_AUDIO",
    "SHUTDOWN_TIMEOUT_SECONDS",
    "MAX_CONCURRENT_SESSIONS",
    "MAX_CONCURRENT_RUNS",
    "MAX_CONCURRENT_RUNS_PER_DEVICE",
    "MAX_CONCURRENT_UPLOADS",
    "MAX_QUEUED_UPLOADS",
    "SESSION_IDLE_TIMEOUT_SECONDS",
    "SESSION_REAPER_INTERVAL_SECONDS",
    "RATE_LIMIT_RUN_PER_MINUTE",
    "RATE_LIMIT_RUN_BURST",
    "RATE_LIMIT_UPLOAD_PER_MINUTE",
    "RATE_LIMIT_UPLOAD_BURST",
    "RATE_LIMIT_DOWNLOAD_PER_MINUTE",
    "RATE_LIMIT_DOWNLOAD_BURST",
    "RATE_LIMIT_MAX_DEVICES",
    "MAX_SSE_FRAME_BYTES",
    "SSE_IDLE_TIMEOUT_SECONDS",
    "SSE_MAX_RECONNECT_ATTEMPTS",
    "BARGE_IN_MIN_SPEECH_MS",
    "BARGE_IN_COOLDOWN_MS",
    "REJECT_RETRY_AFTER_SECONDS",
    "INBOX_RETENTION_DAYS",
    "OUTBOX_RETENTION_DAYS",
    "TRANSCRIPT_RETENTION_DAYS",
)


def _outside_git_work_tree(candidate: Path) -> bool:
    resolved = candidate.resolve()
    return not any((parent / ".git").exists() for parent in (resolved, *resolved.parents))


def pytest_configure(config: pytest.Config) -> None:
    """Keep temporary test data out of any enclosing git work tree.

    Some hosts place the system temporary directory inside a dotfiles git
    checkout.  The token store intentionally refuses state files inside git
    work trees, so the test suite uses a truly external temporary root.
    """
    if config.option.basetemp:
        return
    for candidate in (
        Path(tempfile.gettempdir()),
        Path("/tmp"),  # noqa: S108 - conventional external temporary root
        Path("/var/tmp"),  # noqa: S108 - conventional external temporary root
    ):
        if _outside_git_work_tree(candidate):
            config.option.basetemp = str(candidate / "hermes-talk-pytest")
            return


@pytest.fixture(autouse=True)
def isolate_settings_and_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Keep tests independent of ambient env and leaked log handlers."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    reset_settings_cache()
    reset_registered_secrets()
    reset_redaction()
    reset_audit()

    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in handlers and getattr(handler, "_relay_managed", False):
                handler.close()
        root.handlers[:] = handlers
        root.setLevel(level)
        reset_registered_secrets()
        reset_redaction()
        reset_audit()
        reset_settings_cache()


@pytest.fixture
def make_app() -> Callable[..., FastAPI]:
    """Build an app from explicit settings, ignoring any ``.env`` file."""

    def _make_app(**overrides: Any) -> FastAPI:
        settings = Settings(_env_file=None, **overrides)
        return create_app(settings)

    return _make_app
