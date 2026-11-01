"""Append-only audit trail for approvals, auth, attachments, and runs.

Audit entries are JSON objects, one per line.  Writes use ``O_APPEND`` and a
single ``os.write`` call; earlier bytes are never rewritten.  Rotation renames
whole files, never truncates the active audit log.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from relay.config import Settings
from relay.logging.core import get_correlation_id
from relay.logging.redact import (
    path_is_inside_repo,
    redact_value,
    scrub,
)

_AUDIT_LOCK = threading.Lock()
_AUDIT_LOG: AuditLog | None = None
_ALLOWED_DECISIONS = frozenset({"approved", "denied", "timeout"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso8601(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


class AuditLog:
    """A single append-only audit sink with bounded rotation."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int | None = None,
        backup_count: int = 0,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backup_count = max(backup_count, 0)
        self._clock = clock
        self._lock = threading.RLock()
        self._disabled = path_is_inside_repo(self.path)
        if not self._disabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        """Append one redacted JSON entry and return it."""
        correlation_id = get_correlation_id()
        payload: dict[str, Any] = {
            "ts": _iso8601(self._clock()),
            "event": scrub(event),
            "schema_version": 1,
            "correlation_id": scrub(correlation_id) if correlation_id is not None else None,
        }
        for key, value in fields.items():
            payload[str(key)] = redact_value(value, key=str(key))

        line = (
            json.dumps(
                payload,
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )
            + "\n"
        )

        if self._disabled:
            return payload

        with self._lock:
            self._rotate_if_needed_locked()
            try:
                descriptor = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    0o600,
                )
                try:
                    os.write(descriptor, line.encode("utf-8"))
                    with suppress(OSError):
                        os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                with suppress(OSError):
                    os.chmod(self.path, 0o600)
            except OSError:
                # Audit is best-effort; a full disk must not stop the relay.
                pass
        return payload

    def rotate_if_needed(self) -> None:
        """Rotate the active file when it exceeds ``max_bytes``."""
        with self._lock:
            self._rotate_if_needed_locked()

    def _rotate_if_needed_locked(self) -> None:
        if self.max_bytes is None or self.max_bytes <= 0:
            return
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size <= self.max_bytes:
            return
        target = self._unique_rotation_path()
        try:
            os.replace(self.path, target)
        except OSError:
            return
        self._enforce_backup_cap()

    def _unique_rotation_path(self) -> Path:
        timestamp = self._clock()
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        stamp = timestamp.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        candidate = self.path.with_name(f"{self.path.name}.{stamp}")
        counter = 1
        while candidate.exists():
            candidate = self.path.with_name(f"{self.path.name}.{stamp}-{counter}")
            counter += 1
        return candidate

    def _enforce_backup_cap(self) -> None:
        rotated = sorted(
            (item for item in self.path.parent.glob(f"{self.path.name}.*") if item.is_file()),
            key=lambda item: item.stat().st_mtime,
        )
        while len(rotated) > self.backup_count:
            oldest = rotated.pop(0)
            with suppress(OSError):
                oldest.unlink()

    def read_entries(self) -> list[dict[str, Any]]:
        """Parse the active audit file line by line."""
        entries: list[dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        entries.append(json.loads(stripped))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return entries
        return entries


def configure_audit(settings: Settings) -> AuditLog | None:
    """Configure the process-wide audit sink, or disable it."""
    global _AUDIT_LOG
    path = settings.audit_log_path
    if path is None or path_is_inside_repo(path):
        with _AUDIT_LOCK:
            _AUDIT_LOG = None
        return None
    sink = AuditLog(
        path,
        max_bytes=settings.audit_max_bytes,
        backup_count=settings.audit_backup_count,
    )
    with _AUDIT_LOCK:
        _AUDIT_LOG = sink
    return sink


def get_audit_log() -> AuditLog | None:
    """Return the configured audit sink, if any."""
    with _AUDIT_LOCK:
        return _AUDIT_LOG


def reset_audit() -> None:
    """Disable the process-wide audit sink (tests and shutdown)."""
    global _AUDIT_LOG
    with _AUDIT_LOCK:
        _AUDIT_LOG = None


def audit(event: str, **fields: Any) -> dict[str, Any] | None:
    """Emit an audit entry, or return ``None`` when no sink is configured."""
    sink = get_audit_log()
    if sink is None:
        return None
    return sink.emit(event, **fields)


def _secret_text(value: str | SecretStr | None) -> str:
    if value is None:
        return ""
    if isinstance(value, SecretStr):
        with suppress(Exception):
            return value.get_secret_value()
        return ""
    return str(value)


def _clip_prefix(value: str | SecretStr | None, limit: int = 8) -> str | None:
    text = _secret_text(value)
    if not text:
        return None
    return text[:limit]


def audit_auth_failure(
    reason: str,
    token_prefix: str | SecretStr | None = None,
    device_label: str | None = None,
) -> dict[str, Any] | None:
    """Record an authentication failure without the full token."""
    return audit(
        "auth.failure",
        reason=reason,
        token_prefix=_clip_prefix(token_prefix, 8),
        device_label=device_label,
    )


def audit_token_issued(
    token_id: str | SecretStr,
    device_label: str | None = None,
) -> dict[str, Any] | None:
    """Record a newly issued device token by id (never the raw token)."""
    return audit("token.issued", token_id=token_id, device_label=device_label)


def audit_token_revoked(
    token_id: str | SecretStr,
    device_label: str | None = None,
) -> dict[str, Any] | None:
    """Record a revoked device token by id (never the raw token)."""
    return audit("token.revoked", token_id=token_id, device_label=device_label)


def audit_attachment(
    accepted: bool,
    sha256: str,
    mime: str,
    size_bytes: int,
    reason: str | None = None,
    filename: str | None = None,
) -> dict[str, Any] | None:
    """Record an attachment decision with a basename-only filename."""
    name = Path(filename).name if filename else None
    event = "attachment.accepted" if accepted else "attachment.rejected"
    return audit(
        event,
        accepted=accepted,
        sha256=sha256,
        mime=mime,
        size_bytes=size_bytes,
        reason=reason,
        filename=name,
    )


def audit_approval_requested(
    prompt_id: str,
    execution_generation: int,
    transcript_sha256: str,
    tool: str | None = None,
    summary: str | None = None,
) -> dict[str, Any] | None:
    """Record a pending approval request without transcript text."""
    return audit(
        "approval.requested",
        prompt_id=prompt_id,
        execution_generation=execution_generation,
        transcript_sha256=transcript_sha256,
        tool=tool,
        summary=summary,
    )


def audit_approval_decided(
    prompt_id: str,
    execution_generation: int,
    decision: str,
    transcript_sha256: str | None = None,
    actor: str | None = None,
) -> dict[str, Any] | None:
    """Record an approval decision: approved, denied, or timeout."""
    normalized = str(decision).strip().lower()
    if normalized not in _ALLOWED_DECISIONS:
        msg = f"decision must be one of {sorted(_ALLOWED_DECISIONS)}"
        raise ValueError(msg)
    return audit(
        "approval.decided",
        prompt_id=prompt_id,
        execution_generation=execution_generation,
        decision=normalized,
        transcript_sha256=transcript_sha256,
        actor=actor,
    )


def audit_run_started(
    run_id: str,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    """Record the start of a Hermes run."""
    return audit("run.started", run_id=run_id, session_id=session_id)


def audit_run_stopped(run_id: str, status: str) -> dict[str, Any] | None:
    """Record the stop of a Hermes run."""
    return audit("run.stopped", run_id=run_id, status=status)


def transcript_hash(text: str) -> str:
    """Return the SHA-256 hex digest of transcript text."""
    if isinstance(text, SecretStr):
        text = _secret_text(text)
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    return hashlib.sha256(data).hexdigest()


def token_prefix(token: str | SecretStr) -> str:
    """Return the first four characters of a token followed by an ellipsis."""
    text = _secret_text(token)
    return text[:4] + "…"


def sha256_hex(payload: bytes | bytearray | memoryview) -> str:
    """Return the SHA-256 hex digest of a bytes-like value."""
    return hashlib.sha256(bytes(payload)).hexdigest()
