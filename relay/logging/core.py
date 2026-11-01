"""Structured logging configuration and correlation identifiers.

The public helpers live here for compatibility with the original single-module
``relay.logging`` import surface.  Every handler receives a
:class:`~relay.logging.redact.RedactingFilter`, and file sinks are only created
when ``DATA_DIR`` points outside the repository.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from contextlib import suppress
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from relay.config import Settings
from relay.logging.redact import (
    RESERVED_KEYS,
    STANDARD_RECORD_KEYS,
    RedactingFilter,
    configure_redaction,
    path_is_inside_repo,
    redact_value,
    register_secret,
    register_secrets,
)

_SERVICE = "voice-relay"
_CORRELATION_ID: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_ACCESS_LOG_LEVELS = {"/health": logging.DEBUG, "/ready": logging.DEBUG}


def set_correlation_id(correlation_id: str) -> Token[str | None]:
    """Bind a correlation id to the current context."""
    return _CORRELATION_ID.set(correlation_id)


def get_correlation_id() -> str | None:
    """Return the correlation id bound to the current context, if any."""
    return _CORRELATION_ID.get()


def reset_correlation_id(token: Token[str | None]) -> None:
    """Restore the correlation id context to the state before ``token``."""
    _CORRELATION_ID.reset(token)


class JsonFormatter(logging.Formatter):
    """Render one JSON object per log line on stdout."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": get_correlation_id(),
            "service": _SERVICE,
        }
        fields: dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key in RESERVED_KEYS or key in STANDARD_RECORD_KEYS or key.startswith("_"):
                continue
            fields[key] = value
        if record.exc_text:
            fields["exc_info"] = record.exc_text
        elif record.exc_info:
            fields["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            fields["stack_info"] = record.stack_info
        if fields:
            payload["fields"] = fields
        return json.dumps(payload, default=str, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    """Plain, human-readable log format for local development."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        correlation_id = get_correlation_id()
        if correlation_id is None:
            return rendered
        return f"{rendered} [correlation_id={correlation_id}]"


def _formatter_for(log_format: str) -> logging.Formatter:
    if log_format == "text":
        return HumanFormatter()
    return JsonFormatter()


def _level_for(log_level: str) -> int:
    return logging.getLevelNamesMapping().get(log_level.upper(), logging.INFO)


def _ensure_redacting_filter(handler: logging.Handler) -> None:
    if not any(isinstance(item, RedactingFilter) for item in handler.filters):
        handler.addFilter(RedactingFilter())


def _harden_existing_handlers() -> None:
    """Redact through every sink already attached to the root logger.

    Records propagate to ancestor handlers without passing the ancestor
    logger's filters, so a handler installed before us (pytest's caplog,
    a third-party sink) would otherwise format the raw record.
    """
    root = logging.getLogger()
    if not any(isinstance(item, RedactingFilter) for item in root.filters):
        root.addFilter(RedactingFilter())
    for handler in root.handlers:
        _ensure_redacting_filter(handler)


def _register_settings_secrets(settings: Settings) -> None:
    register_secret(settings.hermes_api_key)
    register_secret(settings.hermes_session_key)
    # Invalid DEVICE_TOKENS must not prevent logging from starting.
    with suppress(ValueError):
        register_secrets(settings.device_tokens)


def _managed_handlers(kind: str) -> list[logging.Handler]:
    return [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_relay_managed", False)
        and getattr(handler, "_relay_kind", "stream") == kind
    ]


def _remove_managed_handlers(kind: str) -> None:
    root = logging.getLogger()
    for handler in _managed_handlers(kind):
        root.removeHandler(handler)
        handler.close()


def _configure_stream_handler(
    formatter: logging.Formatter,
    level: int,
) -> None:
    existing = _managed_handlers("stream")
    if existing:
        for handler in existing:
            handler.setLevel(level)
            handler.setFormatter(formatter)
            _ensure_redacting_filter(handler)
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(formatter)
    handler._relay_managed = True  # type: ignore[attr-defined]
    handler._relay_kind = "stream"  # type: ignore[attr-defined]
    _ensure_redacting_filter(handler)
    logging.getLogger().addHandler(handler)


def _configure_file_handler(
    settings: Settings,
    formatter: logging.Formatter,
    level: int,
) -> None:
    log_dir = settings.log_dir
    if log_dir is None:
        _remove_managed_handlers("file")
        return

    log_path = log_dir / "relay.log"
    if path_is_inside_repo(log_path):
        # Acceptance rule: no log file is ever created inside the repository.
        _remove_managed_handlers("file")
        return

    log_dir.mkdir(parents=True, exist_ok=True)
    resolved = log_path.resolve()
    existing = _managed_handlers("file")
    matching = [
        handler
        for handler in existing
        if Path(getattr(handler, "baseFilename", "")).resolve() == resolved
    ]
    for handler in existing:
        if handler not in matching:
            logging.getLogger().removeHandler(handler)
            handler.close()
    if matching:
        for handler in matching:
            handler.setLevel(level)
            handler.setFormatter(formatter)
            _ensure_redacting_filter(handler)
        return

    handler = logging.handlers.RotatingFileHandler(
        str(log_path),
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    handler._relay_managed = True  # type: ignore[attr-defined]
    handler._relay_kind = "file"  # type: ignore[attr-defined]
    _ensure_redacting_filter(handler)
    logging.getLogger().addHandler(handler)


def configure_logging(settings: Settings) -> None:
    """Configure bounded stdout/file logging and the audit sink.

    Secrets from ``settings`` are registered for scrubbing before any handler
    is attached, so even the first log line is safe.  The file sink is skipped
    entirely when ``DATA_DIR`` is unset or resolves inside the repository.
    """
    configure_redaction(settings)
    _register_settings_secrets(settings)

    # Imported lazily to avoid a circular import with audit.get_correlation_id.
    from relay.logging.audit import configure_audit

    configure_audit(settings)

    root = logging.getLogger()
    level = _level_for(settings.log_level)
    root.setLevel(level)
    formatter = _formatter_for(settings.log_format)

    _configure_stream_handler(formatter, level)
    _configure_file_handler(settings, formatter, level)
    _harden_existing_handlers()


def log_request(
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    client_host: str | None = None,
) -> None:
    """Log a single access record.  Headers and bodies are never logged."""
    safe_path = redact_value(path)
    safe_client_host = redact_value(client_host)
    access_logger = logging.getLogger("relay.access")
    level = _ACCESS_LOG_LEVELS.get(path, logging.INFO)
    access_logger.log(
        level,
        "%s %s -> %s",
        method,
        safe_path,
        status_code,
        extra={
            "method": method,
            "path": safe_path,
            "status_code": status_code,
            "duration_ms": round(duration_ms, 3),
            "client_host": safe_client_host,
        },
    )
