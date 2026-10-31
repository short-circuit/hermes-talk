"""Shared pytest fixtures for the relay test suite."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from relay.app import create_app
from relay.config import Settings, reset_settings_cache

_ENV_KEYS = (
    "RELAY_HOST",
    "RELAY_PORT",
    "HERMES_BASE_URL",
    "HERMES_API_KEY",
    "LOCALAI_BASE_URL",
    "VOICE_BRAIN_MODEL",
    "STT_MODEL",
    "TTS_MODEL",
    "VAD_MODEL",
    "DEVICE_TOKENS",
    "INBOX_DIR",
    "OUTBOX_DIR",
    "DATA_DIR",
    "MAX_ATTACHMENT_MB",
    "MAX_CALL_MINUTES",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "SHUTDOWN_TIMEOUT_SECONDS",
)


@pytest.fixture(autouse=True)
def isolate_settings_and_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Keep tests independent of ambient env and leaked log handlers."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    reset_settings_cache()

    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        reset_settings_cache()


@pytest.fixture
def make_app() -> Callable[..., FastAPI]:
    """Build an app from explicit settings, ignoring any ``.env`` file."""

    def _make_app(**overrides: Any) -> FastAPI:
        settings = Settings(_env_file=None, **overrides)
        return create_app(settings)

    return _make_app
