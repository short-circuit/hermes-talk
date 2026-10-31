"""Structured logging, correlation ids, and lightweight redaction helpers."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Mapping
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

from relay.config import Settings

_SERVICE = "voice-relay"
_CORRELATION_ID: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_RESERVED_KEYS = frozenset(
    {"ts", "level", "logger", "message", "correlation_id", "service", "fields"}
)
_STANDARD_RECORD_KEYS = frozenset(
    {
        "args",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)
_SENSITIVE_KEYS = frozenset({"authorization", "api_key", "token", "tokens", "session_key"})
_BEARER_TOKEN_RE = re.compile(r"^\s*bearer\s+\S+\s*$", re.IGNORECASE)
_MAX_STRING_LENGTH = 512
_TRUNCATION_SUFFIX = "…[truncated]"
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
            if key in _RESERVED_KEYS or key in _STANDARD_RECORD_KEYS or key.startswith("_"):
                continue
            fields[key] = value
        if record.exc_info:
            fields["exc_info"] = self.formatException(record.exc_info)
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


def configure_logging(settings: Settings) -> None:
    """Configure stdout logging once, without writing files into the repo."""
    root = logging.getLogger()
    root.setLevel(_level_for(settings.log_level))
    formatter = _formatter_for(settings.log_format)

    for handler in root.handlers:
        if getattr(handler, "_relay_managed", False):
            handler.setLevel(root.level)
            handler.setFormatter(formatter)
            return

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(root.level)
    handler.setFormatter(formatter)
    handler._relay_managed = True  # type: ignore[attr-defined]
    root.addHandler(handler)


def redact(value: Any, *, key: str | None = None) -> Any:
    """Mask sensitive values and truncate long strings."""
    if key is not None and key.lower() in _SENSITIVE_KEYS:
        return "***"
    if not isinstance(value, str):
        return value
    if _BEARER_TOKEN_RE.match(value):
        return "***"
    if len(value) > _MAX_STRING_LENGTH:
        return value[:_MAX_STRING_LENGTH] + _TRUNCATION_SUFFIX
    return value


def redact_headers(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``mapping`` with sensitive headers redacted."""
    return {str(name): redact(value, key=str(name)) for name, value in mapping.items()}


def log_request(
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    client_host: str | None = None,
) -> None:
    """Log a single access record.  Headers and bodies are never logged."""
    access_logger = logging.getLogger("relay.access")
    level = _ACCESS_LOG_LEVELS.get(path, logging.INFO)
    access_logger.log(
        level,
        "%s %s -> %s",
        method,
        path,
        status_code,
        extra={
            "method": method,
            "path": path,
            "status_code": status_code,
            "duration_ms": round(duration_ms, 3),
            "client_host": client_host,
        },
    )
