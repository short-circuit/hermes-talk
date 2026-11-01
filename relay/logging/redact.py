"""Redaction primitives for the relay logging boundary.

Everything here is local-only and defensive: no helper raises, and every
string that leaves the process passes through :func:`scrub` or
:func:`redact_value`.  The module keeps a small registry of runtime secrets
(API keys, device tokens, session keys) so they can be masked even when they
appear inside larger messages or tracebacks.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import traceback
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from relay.config import Settings

_SERVICE = "voice-relay"
_FALLBACK_MAX_CHARS = 512
_TRUNCATION_TEMPLATE = "…[truncated {count} chars]"
_SECRET_MASK = "***"  # noqa: S105 - a mask, not a credential
_UNRENDERABLE = "<unrenderable>"
_MAX_DEPTH = 8

# Standard ``logging.LogRecord`` attributes.  Anything else is an ``extra``.
STANDARD_RECORD_KEYS = frozenset(
    {
        "args",
        "asctime",
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
# Payload keys owned by the JSON formatter.
RESERVED_KEYS = frozenset(
    {"ts", "level", "logger", "message", "correlation_id", "service", "fields"}
)

_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "proxy_authorization",
        "cookie",
        "set-cookie",
        "set_cookie",
        "api_key",
        "api-key",
        "x-api-key",
        "x_api_key",
        "token",
        "tokens",
        "device_token",
        "device-token",
        "session_key",
        "session-key",
        "x-hermes-session-key",
        "x_hermes_session_key",
        "secret",
        "password",
    }
)

_BEARER_RE = re.compile(r"(?i)\bbearer\s+\S+")
_ABS_HOME_RE = re.compile(r"/(?:home|Users)/[^/\s]+")
_KEY_VALUE_RE = re.compile(
    r"(?i)(x-hermes-session-key|authorization|api[_-]?key|session[_-]?key|token)"
    r"\s*[:=]\s*[^\s,;]+"
)

_SECRETS_LOCK = threading.Lock()
_REGISTERED_SECRETS: set[str] = set()


@dataclass
class RedactionConfig:
    """Runtime redaction state, populated by :func:`configure_redaction`."""

    transcript_max_chars: int | None = None
    data_dir: Path | None = None
    home_prefix: Path | None = None


_CONFIG = RedactionConfig()
_CONFIG_LOCK = threading.Lock()


def _coerce_secret(value: str | SecretStr | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, SecretStr):
        try:
            value = value.get_secret_value()
        except Exception:
            return None
    text = str(value)
    if not text.strip():
        return None
    return text


def register_secret(value: str | SecretStr | None) -> None:
    """Register one literal secret for scrubbing (blank values are ignored)."""
    text = _coerce_secret(value)
    if text is None:
        return
    with _SECRETS_LOCK:
        _REGISTERED_SECRETS.add(text)


def register_secrets(values: Iterable[str | SecretStr | None]) -> None:
    """Register every non-blank secret in ``values``."""
    for value in values:
        register_secret(value)


def reset_registered_secrets() -> None:
    """Forget every registered secret (tests and reloads)."""
    with _SECRETS_LOCK:
        _REGISTERED_SECRETS.clear()


def registered_secret_count() -> int:
    """Number of literals currently registered for scrubbing."""
    with _SECRETS_LOCK:
        return len(_REGISTERED_SECRETS)


def _registered_secrets() -> tuple[str, ...]:
    with _SECRETS_LOCK:
        return tuple(_REGISTERED_SECRETS)


def _home_prefix() -> Path | None:
    with _CONFIG_LOCK:
        configured = _CONFIG.home_prefix
    if configured is not None:
        return configured
    try:
        return Path("~").expanduser()
    except Exception:
        return None


def repo_root() -> Path:
    """Repository root derived from this package's location."""
    return Path(__file__).resolve().parents[2]


def path_is_inside_repo(path: Path | str) -> bool:
    """Return True when ``path`` resolves inside the repository tree."""
    try:
        Path(path).resolve().relative_to(repo_root())
    except (OSError, ValueError):
        return False
    return True


def _replace_path_prefixes(text: str) -> str:
    """Remove absolute DATA_DIR/home prefixes from free text."""
    with _CONFIG_LOCK:
        data_dir = _CONFIG.data_dir
    if data_dir is not None:
        prefix = str(data_dir)
        for suffix in (prefix + os.sep, prefix + "/"):
            if suffix != prefix:
                text = text.replace(suffix, "")
        text = text.replace(prefix, ".")

    home = _home_prefix()
    if home is not None:
        prefix = str(home)
        for suffix in (prefix + os.sep, prefix + "/"):
            if suffix != prefix:
                text = text.replace(suffix, "~/")
        text = text.replace(prefix, "~")
    # A path under another user's home is still personal; keep the basename.
    return _ABS_HOME_RE.sub("~", text)


def scrub(text: str) -> str:
    """Mask registered secrets, bearer tokens, key/value pairs and path roots.

    The function is deliberately total: it accepts anything string-like and
    never raises.  Regexes are linear, and registered-secret replacement uses
    ``str.replace`` so long messages stay safe.
    """
    try:
        if not isinstance(text, str):
            text = str(text)
        for secret in sorted(_registered_secrets(), key=len, reverse=True):
            if secret and secret in text:
                text = text.replace(secret, _SECRET_MASK)
        text = _BEARER_RE.sub("Bearer ***", text)
        text = _KEY_VALUE_RE.sub("key=***", text)
        text = _replace_path_prefixes(text)
        return text
    except Exception:
        return _UNRENDERABLE


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        max_chars = 1
    if len(text) <= max_chars:
        return text
    removed = len(text) - max_chars
    return text[:max_chars] + _TRUNCATION_TEMPLATE.format(count=removed)


def _default_max_chars() -> int:
    with _CONFIG_LOCK:
        configured = _CONFIG.transcript_max_chars
    if configured is None:
        return _FALLBACK_MAX_CHARS
    return configured


def redact_text(text: str, *, max_chars: int | None = None) -> str:
    """Scrub and bound a free-text value before it reaches a sink."""
    limit = _default_max_chars() if max_chars is None else max_chars
    return _truncate(scrub(text), limit)


def _is_sensitive_key(key: str | None) -> bool:
    if key is None:
        return False
    return str(key).strip().lower() in _SENSITIVE_KEYS


def _redact_bytes(payload: bytes | bytearray | memoryview) -> str:
    try:
        raw = bytes(payload)
        digest = hashlib.sha256(raw).hexdigest()[:12]
        return f"<{len(raw)} bytes sha256={digest}>"
    except Exception:
        return _UNRENDERABLE


def _redact_value(value: Any, *, key: str | None, max_chars: int | None, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        return "<max depth>"
    if _is_sensitive_key(key):
        return _SECRET_MASK
    if isinstance(value, SecretStr):
        return _SECRET_MASK
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _redact_bytes(value)
    if isinstance(value, str):
        limit = _default_max_chars() if max_chars is None else max_chars
        return _truncate(scrub(value), limit)
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact_value(
                item_value,
                key=str(item_key),
                max_chars=max_chars,
                depth=depth + 1,
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _redact_value(item, key=key, max_chars=max_chars, depth=depth + 1) for item in value
        ]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    try:
        limit = _default_max_chars() if max_chars is None else max_chars
        return _truncate(scrub(str(value)), limit)
    except Exception:
        return _UNRENDERABLE


def redact_value(value: Any, *, key: str | None = None, max_chars: int | None = None) -> Any:
    """Return a redacted, bounded, JSON-safe representation of ``value``.

    SecretStr values become ``***`` without ever calling ``get_secret_value``
    on an output path; bytes-like values become a size/hash summary so raw
    audio never reaches a log sink.
    """
    return _redact_value(value, key=key, max_chars=max_chars, depth=0)


def redact_path(path: Path | str, data_dir: Path | str | None) -> str:
    """Render ``path`` relative to DATA_DIR, then HOME, then as-is."""
    try:
        candidate = Path(path)
        if data_dir is not None:
            try:
                return candidate.relative_to(Path(data_dir)).as_posix()
            except ValueError:
                pass
        home = _home_prefix()
        if home is not None:
            try:
                return "~/" + candidate.relative_to(home).as_posix()
            except ValueError:
                pass
        return str(path)
    except Exception:
        return _UNRENDERABLE


def redact_headers(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of a header mapping with sensitive values masked."""
    result: dict[str, Any] = {}
    for name, value in mapping.items():
        header = str(name)
        if _is_sensitive_key(header):
            result[header] = _SECRET_MASK
        else:
            result[header] = redact_value(value, key=header)
    return result


def redact(value: Any, *, key: str | None = None) -> Any:
    """Backwards-compatible alias for :func:`redact_value`."""
    return redact_value(value, key=key)


class RedactingFilter(logging.Filter):
    """Rewrite log records so no handler or formatter sees raw secrets."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_relay_redacted", False):
            return True
        try:
            record.msg = redact_text(record.getMessage(), max_chars=_default_max_chars())
            record.args = ()
            for key, value in list(record.__dict__.items()):
                if key in RESERVED_KEYS or key in STANDARD_RECORD_KEYS or key.startswith("_"):
                    continue
                record.__dict__[key] = redact_value(value, key=key)
            if record.exc_info:
                try:
                    rendered = "".join(traceback.format_exception(*record.exc_info))
                    record.exc_text = scrub(rendered)
                except Exception:
                    record.exc_text = _UNRENDERABLE
                record.exc_info = None
            if record.stack_info:
                record.stack_info = scrub(str(record.stack_info))
            record._relay_redacted = True
        except Exception:
            record.msg = "<redaction failed>"
            record.args = ()
            record.exc_info = None
            record.exc_text = "<redaction failed>"
            record._relay_redacted = True
        return True


def configure_redaction(settings: Settings) -> None:
    """Load transcript bounds and path roots from ``settings``."""
    try:
        home: Path | None = Path("~").expanduser()
    except Exception:
        home = None
    with _CONFIG_LOCK:
        _CONFIG.transcript_max_chars = settings.log_transcript_max_chars
        _CONFIG.data_dir = settings.data_dir
        _CONFIG.home_prefix = home


def reset_redaction() -> None:
    """Reset redaction configuration (intended for tests)."""
    with _CONFIG_LOCK:
        _CONFIG.transcript_max_chars = None
        _CONFIG.data_dir = None
        _CONFIG.home_prefix = None
