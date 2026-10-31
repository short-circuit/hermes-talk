"""In-process tests for the relay service shell.

These tests drive the ASGI app with ``httpx.ASGITransport``; no sockets are
opened and no network calls are made.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import re
import socket
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr, ValidationError
from relay import __version__
from relay.config import Settings
from relay.logging import JsonFormatter

_BASE_URL = "http://relay.test"
_HEX_ID_RE = re.compile(r"^[0-9a-f]{32}$")


async def _get(
    app: FastAPI,
    path: str,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL) as client:
        return await client.get(path, headers=headers)


class _CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def _capture_root() -> tuple[_CaptureHandler, logging.Logger]:
    handler = _CaptureHandler()
    handler.setFormatter(JsonFormatter())
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    return handler, root


def test_health_is_liveness_only_without_configuration(
    make_app: Callable[..., FastAPI],
) -> None:
    app = make_app()
    response = asyncio.run(_get(app, "/health"))

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "ok",
        "service": "voice-relay",
        "version": __version__,
    }


def test_health_touches_no_filesystem_or_network(
    make_app: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app()

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unexpected filesystem or network access")

    monkeypatch.setattr(pathlib.Path, "exists", boom)
    monkeypatch.setattr(socket, "create_connection", boom)

    response = asyncio.run(_get(app, "/health"))
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ready_reports_missing_settings_and_auth(
    make_app: Callable[..., FastAPI],
) -> None:
    app = make_app()
    response = asyncio.run(_get(app, "/ready"))

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    checks = {check["name"]: check for check in body["checks"]}
    assert checks["settings"]["ok"] is False
    assert checks["auth"]["ok"] is False
    assert "inbox_dir" in checks["settings"]["detail"]
    assert "outbox_dir" in checks["settings"]["detail"]
    assert "device_tokens" in checks["settings"]["detail"]


def test_ready_is_ready_with_writable_storage_and_token(
    make_app: Callable[..., FastAPI],
    tmp_path: pathlib.Path,
) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    inbox.mkdir()
    outbox.mkdir()
    app = make_app(
        inbox_dir=str(inbox),
        outbox_dir=str(outbox),
        data_dir=str(tmp_path),
        device_tokens_raw="device-token",
    )

    response = asyncio.run(_get(app, "/ready"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert all(check["ok"] for check in body["checks"])
    storage = next(check for check in body["checks"] if check["name"] == "storage")
    assert "inbox" in storage["detail"]
    assert "outbox" in storage["detail"]
    assert str(tmp_path) not in storage["detail"]


def test_ready_fails_when_configured_storage_is_missing(
    make_app: Callable[..., FastAPI],
    tmp_path: pathlib.Path,
) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    app = make_app(
        inbox_dir=str(inbox),
        outbox_dir=str(outbox),
        data_dir=str(tmp_path),
        device_tokens_raw="device-token",
    )

    response = asyncio.run(_get(app, "/ready"))

    assert response.status_code == 503
    body = response.json()
    checks = {check["name"]: check for check in body["checks"]}
    assert checks["storage"]["ok"] is False
    assert "inbox: missing" in checks["storage"]["detail"]
    assert not inbox.exists()
    assert not outbox.exists()


def test_ready_never_opens_a_socket(
    make_app: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    inbox.mkdir()
    outbox.mkdir()
    app = make_app(
        inbox_dir=str(inbox),
        outbox_dir=str(outbox),
        device_tokens_raw="device-token",
    )

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unexpected socket use")

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        monkeypatch.setattr(socket, "socket", boom)
        monkeypatch.setattr(socket, "create_connection", boom)
        response = loop.run_until_complete(_get(app, "/ready"))
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_ready_fails_while_shutting_down(
    make_app: Callable[..., FastAPI],
    tmp_path: pathlib.Path,
) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    inbox.mkdir()
    outbox.mkdir()
    app = make_app(
        inbox_dir=str(inbox),
        outbox_dir=str(outbox),
        device_tokens_raw="device-token",
    )
    app.state.shutting_down = True

    response = asyncio.run(_get(app, "/ready"))

    assert response.status_code == 503
    body = response.json()
    checks = {check["name"]: check for check in body["checks"]}
    assert checks["shutdown"]["ok"] is False
    assert body["status"] == "not_ready"


def test_correlation_id_is_echoed_or_replaced(
    make_app: Callable[..., FastAPI],
) -> None:
    app = make_app()
    valid_id = "abc-123._"

    valid_response = asyncio.run(_get(app, "/health", headers={"X-Correlation-ID": valid_id}))
    assert valid_response.headers["X-Correlation-ID"] == valid_id

    invalid_response = asyncio.run(_get(app, "/health", headers={"X-Correlation-ID": "bad id!"}))
    invalid_id = invalid_response.headers["X-Correlation-ID"]
    assert _HEX_ID_RE.fullmatch(invalid_id)

    long_response = asyncio.run(_get(app, "/health", headers={"X-Correlation-ID": "a" * 65}))
    long_id = long_response.headers["X-Correlation-ID"]
    assert _HEX_ID_RE.fullmatch(long_id)
    assert long_id != "a" * 65


def test_access_log_is_json_with_correlation_id(
    make_app: Callable[..., FastAPI],
) -> None:
    app = make_app()
    handler = _CaptureHandler()
    handler.setFormatter(JsonFormatter())
    handler.setLevel(logging.DEBUG)
    access_logger = logging.getLogger("relay.access")
    previous_level = access_logger.level
    access_logger.addHandler(handler)
    access_logger.setLevel(logging.DEBUG)
    correlation_id = "request-123"
    try:
        response = asyncio.run(_get(app, "/health", headers={"X-Correlation-ID": correlation_id}))
    finally:
        access_logger.removeHandler(handler)
        access_logger.setLevel(previous_level)

    assert response.status_code == 200
    assert handler.lines
    payload = json.loads(handler.lines[-1])
    assert payload["correlation_id"] == correlation_id
    assert payload["service"] == "voice-relay"
    assert payload["fields"]["path"] == "/health"
    assert payload["fields"]["status_code"] == 200


def test_secrets_never_reach_logs(
    make_app: Callable[..., FastAPI],
) -> None:
    hermes_secret = "hermes-secret-value"
    bearer_token = "device-bearer-value"
    app = make_app(hermes_api_key=hermes_secret, log_level="DEBUG")
    handler, root = _capture_root()
    previous_level = root.level
    root.setLevel(logging.DEBUG)

    async def run() -> None:
        async with app.router.lifespan_context(app):
            await _get(
                app,
                "/health",
                headers={"Authorization": f"Bearer {bearer_token}"},
            )

    try:
        asyncio.run(run())
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    output = "\n".join(handler.lines)
    assert hermes_secret not in output
    assert bearer_token not in output
    assert "**********" in repr(app.state.settings.hermes_api_key)


def test_lifespan_sets_and_clears_shutdown_state(
    make_app: Callable[..., FastAPI],
) -> None:
    app = make_app(log_level="DEBUG")
    entries: list[tuple[str, bool]] = []

    class _FlagHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            entries.append((record.getMessage(), bool(app.state.shutting_down)))

    handler = _FlagHandler()
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)

    async def run() -> None:
        async with app.router.lifespan_context(app):
            assert app.state.shutting_down is False
        assert app.state.shutting_down is False

    try:
        asyncio.run(run())
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    assert ("relay.startup", False) in entries
    assert ("relay.shutdown.begin", True) in entries
    assert ("relay.shutdown.complete", True) in entries


def test_settings_reject_invalid_values() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, relay_port=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, relay_port=70000)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, hermes_base_url="ftp://example.test:8642")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, localai_base_url="not-a-url")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_attachment_mb=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, shutdown_timeout_seconds=-1)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, log_format="xml")


def test_device_tokens_are_parsed_as_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVICE_TOKENS", "alpha-secret, beta-secret")
    settings = Settings(_env_file=None)

    tokens = settings.device_tokens
    assert len(tokens) == 2
    assert all(isinstance(token, SecretStr) for token in tokens)
    rendered = repr(tokens)
    assert "alpha-secret" not in rendered
    assert "beta-secret" not in rendered
    assert settings.configured_device_token_count == 2


def test_device_tokens_reject_duplicates_and_blanks() -> None:
    duplicate_settings = Settings(
        _env_file=None,
        device_tokens_raw="same-token, same-token",
    )
    with pytest.raises(ValueError, match="duplicate"):
        _ = duplicate_settings.device_tokens

    blank_settings = Settings(_env_file=None, device_tokens_raw=" , ")
    with pytest.raises(ValueError, match="non-blank"):
        _ = blank_settings.device_tokens


def test_config_helpers_report_storage_and_missing_settings(
    tmp_path: pathlib.Path,
) -> None:
    settings = Settings(
        _env_file=None,
        inbox_dir=str(tmp_path / "inbox"),
        outbox_dir=str(tmp_path / "outbox"),
        data_dir=str(tmp_path),
        device_tokens_raw="token-a, token-b",
    )

    assert settings.storage_paths() == (
        tmp_path / "inbox",
        tmp_path / "outbox",
        tmp_path,
    )
    assert settings.missing_required_settings() == []
    assert settings.configured_device_token_count == 2

    empty = Settings(_env_file=None)
    assert empty.storage_paths() == ()
    assert empty.missing_required_settings() == [
        "device_tokens",
        "inbox_dir",
        "outbox_dir",
    ]
