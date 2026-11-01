"""Offline data-retention planning and execution for relay stores.

This module deletes expired inbox attachments, outbox artifacts, and Hermes API
sessions.  It makes no network calls except the loopback Hermes session API used
for transcript deletion; everything else is local filesystem work.

Active-call safety contract
===========================

A file in the inbox may be in use by a running turn.  Every turn that hands an
attachment to the agent MUST keep a cross-process claim open for the whole turn::

    with ReferenceGuard(inbox_root).hold(attachment_path):
        ... run the turn ...

:class:`ReferenceGuard` implements the claim with ``fcntl.flock`` lock files
under ``<root>/.locks``.  Pruning uses the same lock, so an expired file that is
claimed by a live turn is skipped (``reason="in_use"``) instead of unlinked.
Deletion only happens while the pruner holds the exclusive lock, so a turn
cannot start using a file that is being removed.  Lock files are removed only
by :meth:`ReferenceGuard.release_stale_locks`; a claim never lands on an
unlinked lock file.

Transcripts are deleted exclusively through ``DELETE /api/sessions/{id}``; the
Hermes session database is never written directly.
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Linux platforms
    fcntl = None  # type: ignore[assignment]

from relay.config import Settings
from relay.logging import audit, path_is_inside_repo

DEFAULT_RETENTION_DAYS = 0
_SECONDS_PER_DAY = 86400
_SESSION_PAGE_LIMIT = 200
_MAX_SESSION_PAGES = 100
_API_SERVER_SOURCE = "api_server"
_LOCK_SUFFIX = ".lock"
_LOCKS_DIRNAME = ".locks"
_LOCK_DIR_MODE = 0o700
_LOCK_FILE_MODE = 0o600
_LOCK_ATTEMPTS = 5


class RetentionConfigError(ValueError):
    """A retention setting or path is invalid."""


class HermesUnavailableError(RuntimeError):
    """The Hermes session API could not be reached."""


@dataclass(frozen=True)
class RetentionPolicy:
    """Retention windows, in days, for each relay-owned store."""

    inbox_days: int = 0
    outbox_days: int = 0
    transcript_days: int = 0

    @classmethod
    def from_settings(cls, settings: Settings) -> RetentionPolicy:
        """Build a policy from environment-backed settings."""
        return cls(
            inbox_days=settings.inbox_retention_days,
            outbox_days=settings.outbox_retention_days,
            transcript_days=settings.transcript_retention_days,
        )


@dataclass(frozen=True)
class RetentionItem:
    """One planned or performed retention action."""

    kind: str
    identifier: str
    age_days: int
    reason: str
    size_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe record with no absolute paths."""
        payload: dict[str, Any] = {
            "kind": self.kind,
            "identifier": self.identifier,
            "age_days": self.age_days,
            "reason": self.reason,
        }
        if self.size_bytes is not None:
            payload["size_bytes"] = self.size_bytes
        return payload


@dataclass
class RetentionReport:
    """The outcome of one prune run."""

    deleted: list[RetentionItem] = field(default_factory=list)
    skipped: list[RetentionItem] = field(default_factory=list)
    errors: list[tuple[str, str, str]] = field(default_factory=list)
    dry_run: bool = False
    stale_locks_removed: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe report; every identifier stays relative."""
        return {
            "deleted": [item.to_dict() for item in self.deleted],
            "skipped": [item.to_dict() for item in self.skipped],
            "errors": [
                {"kind": kind, "identifier": identifier, "error": message}
                for kind, identifier, message in self.errors
            ],
            "dry_run": self.dry_run,
            "stale_locks_removed": self.stale_locks_removed,
        }


class ReferenceGuard:
    """A cross-process claim on files under one storage root.

    Lock files live in ``<root>/.locks/<target.name>.lock`` with mode ``0600``;
    the lock directory is mode ``0700``.  A target must resolve inside ``root``.
    Without ``fcntl`` (non-Linux) claims are advisory no-ops and deletion
    degrades to a plain ``os.unlink``.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root).expanduser().resolve()

    @property
    def root(self) -> Path:
        """The resolved storage root this guard protects."""
        return self._root

    @property
    def lock_dir(self) -> Path:
        """The directory holding claim files."""
        return self._root / _LOCKS_DIRNAME

    def _resolve_target(self, target: Path) -> Path:
        try:
            resolved = Path(target).expanduser().resolve()
        except OSError as exc:
            msg = f"cannot resolve target path: {target}"
            raise RetentionConfigError(msg) from exc
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            msg = f"target {resolved} is outside the guarded root {self._root}"
            raise RetentionConfigError(msg) from exc
        return resolved

    def lock_path(self, target: Path) -> Path:
        """Return the claim file for ``target``; reject escapes from the root."""
        resolved = self._resolve_target(target)
        return self.lock_dir / f"{resolved.name}{_LOCK_SUFFIX}"

    def _ensure_lock_dir(self) -> None:
        try:
            self.lock_dir.mkdir(mode=_LOCK_DIR_MODE, parents=True, exist_ok=True)
            os.chmod(self.lock_dir, _LOCK_DIR_MODE)
        except OSError as exc:
            msg = f"cannot prepare the lock directory: {self.lock_dir}"
            raise RetentionConfigError(msg) from exc

    def is_claimed(self, target: Path) -> bool:
        """Return whether another holder currently claims ``target``."""
        resolved = self._resolve_target(target)
        if fcntl is None:
            return False
        path = self.lock_dir / f"{resolved.name}{_LOCK_SUFFIX}"
        if not path.exists():
            return False
        try:
            descriptor = os.open(path, os.O_RDWR)
        except OSError:
            return False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(descriptor)

    def _acquire_stable_descriptor(self, path: Path) -> int:
        """Open and lock ``path``, retrying if the path was replaced.

        The descriptor returned by ``os.open`` is compared with the current
        path after the blocking ``flock`` succeeds.  A mismatch means another
        process removed the lock file while this process was waiting, so the
        descriptor is discarded and the claim is retried against the new file.
        """
        for _attempt in range(_LOCK_ATTEMPTS):
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, _LOCK_FILE_MODE)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                on_disk = os.stat(path)
            except FileNotFoundError:
                os.close(descriptor)
                continue
            except OSError:
                os.close(descriptor)
                raise
            held = os.fstat(descriptor)
            if (held.st_dev, held.st_ino) != (on_disk.st_dev, on_disk.st_ino):
                os.close(descriptor)
                continue
            return descriptor
        msg = "lock_file_unstable"
        raise RetentionConfigError(msg)

    @contextmanager
    def hold(self, target: Path) -> Iterator[None]:
        """Claim ``target`` for the duration of a turn.

        The claim is a blocking exclusive ``flock``; closing the descriptor in
        the ``finally`` block releases it.  Re-entrancy within one process is
        not required.  If the lock file is replaced while the claim is being
        acquired, the claim is retried against the new file (at most five
        attempts) instead of landing on an unlinked inode.
        """
        resolved = self._resolve_target(target)
        if fcntl is None:  # pragma: no cover - non-Linux platforms
            yield
            return
        self._ensure_lock_dir()
        path = self.lock_dir / f"{resolved.name}{_LOCK_SUFFIX}"
        descriptor = self._acquire_stable_descriptor(path)
        try:
            yield
        finally:
            os.close(descriptor)

    def unlink_if_unclaimed(self, target: Path) -> bool:
        """Unlink ``target`` only when no live turn holds its claim.

        Returns ``True`` when the file was removed, ``False`` when it is
        claimed or already missing.  The unlink happens while the exclusive
        lock is held, so no turn can begin using the file mid-deletion.  The
        lock file itself is left for :meth:`release_stale_locks`.
        """
        resolved = self._resolve_target(target)
        if fcntl is None:  # pragma: no cover - non-Linux platforms
            try:
                os.unlink(resolved)
            except FileNotFoundError:
                return False
            return True
        self._ensure_lock_dir()
        path = self.lock_dir / f"{resolved.name}{_LOCK_SUFFIX}"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, _LOCK_FILE_MODE)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            try:
                os.unlink(resolved)
            except FileNotFoundError:
                return False
            return True
        finally:
            os.close(descriptor)

    def release_stale_locks(self) -> int:
        """Delete claim files whose target file is gone; return the count.

        A lock file is unlinked only while its exclusive lock is held, so a
        live holder is never robbed of its claim file.
        """
        if fcntl is None:  # pragma: no cover - non-Linux platforms
            return 0
        if not self.lock_dir.is_dir():
            return 0
        existing: set[str] = set()
        for dirpath, dirnames, filenames in os.walk(self._root, followlinks=False):
            if Path(dirpath) == self._root and _LOCKS_DIRNAME in dirnames:
                dirnames.remove(_LOCKS_DIRNAME)
            existing.update(filenames)
        removed = 0
        try:
            entries = list(os.scandir(self.lock_dir))
        except OSError:
            return removed
        for entry in entries:
            if not entry.name.endswith(_LOCK_SUFFIX):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            target_name = entry.name[: -len(_LOCK_SUFFIX)]
            if target_name in existing:
                continue
            try:
                descriptor = os.open(entry.path, os.O_RDWR)
            except OSError:
                continue
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                try:
                    os.unlink(entry.path)
                    removed += 1
                except OSError:
                    continue
            finally:
                os.close(descriptor)
        return removed


def _coerce_now(now: datetime | None) -> datetime:
    current = now if now is not None else datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current


def _relative_posix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _iter_files(root: Path) -> Iterator[Path]:
    """Yield regular files under ``root`` without following symlinks."""
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name == _LOCKS_DIRNAME and Path(entry.path).parent == root:
                    continue
                yield from _iter_files(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                yield Path(entry.path)
        except OSError:
            continue


def _audit_deleted(item: RetentionItem) -> None:
    fields: dict[str, Any] = {
        "kind": item.kind,
        "age_days": item.age_days,
        "reason": item.reason,
    }
    if item.kind == "transcript":
        fields["session_id"] = item.identifier
    else:
        fields["path"] = item.identifier
        fields["size_bytes"] = item.size_bytes
    audit("retention.deleted", **fields)


def _audit_skipped(item: RetentionItem) -> None:
    fields: dict[str, Any] = {
        "kind": item.kind,
        "age_days": item.age_days,
        "reason": item.reason,
    }
    if item.kind == "transcript":
        fields["session_id"] = item.identifier
    else:
        fields["path"] = item.identifier
    audit("retention.skipped", **fields)


def _audit_error(kind: str, identifier: str, message: str) -> None:
    fields: dict[str, Any] = {"kind": kind, "error": message}
    if kind == "transcript":
        fields["session_id"] = identifier
    else:
        fields["path"] = identifier
    audit("retention.error", **fields)


def _emit_summary(report: RetentionReport) -> None:
    audit(
        "retention.summary",
        deleted=len(report.deleted),
        skipped=len(report.skipped),
        errors=len(report.errors),
        dry_run=report.dry_run,
        stale_locks_removed=report.stale_locks_removed,
    )


def _error_message(exc: OSError) -> str:
    # ``str(OSError)`` may embed an absolute path; keep only the reason.
    return exc.strerror or exc.__class__.__name__


def _prune_files(
    kind: str,
    root: Path | None,
    days: int,
    *,
    now: datetime | None,
    dry_run: bool,
    guard: ReferenceGuard | None,
    emit_summary: bool,
) -> RetentionReport:
    report = RetentionReport(dry_run=dry_run)
    if days <= 0:
        return report
    if root is None:
        return report
    candidate = Path(root).expanduser()
    if path_is_inside_repo(candidate):
        message = "root_inside_repo"
        report.errors.append((kind, ".", message))
        if not dry_run:
            _audit_error(kind, ".", message)
        if emit_summary:
            _emit_summary(report)
        return report
    try:
        resolved_root = candidate.resolve()
    except OSError:
        return report
    if not resolved_root.is_dir():
        return report

    active_guard = guard or ReferenceGuard(resolved_root)
    current = _coerce_now(now)
    now_ts = current.timestamp()
    threshold = days * _SECONDS_PER_DAY

    for target in _iter_files(resolved_root):
        try:
            stat_result = target.stat()
        except OSError as exc:
            identifier = _relative_posix(target, resolved_root)
            message = _error_message(exc)
            report.errors.append((kind, identifier, message))
            if not dry_run:
                _audit_error(kind, identifier, message)
            continue

        age_seconds = now_ts - stat_result.st_mtime
        age_days = int(age_seconds // _SECONDS_PER_DAY)
        if age_seconds < threshold:
            continue

        identifier = _relative_posix(target, resolved_root)
        item = RetentionItem(
            kind=kind,
            identifier=identifier,
            age_days=age_days,
            reason="age",
            size_bytes=stat_result.st_size,
        )

        if dry_run:
            if active_guard.is_claimed(target):
                skipped = RetentionItem(
                    kind=kind,
                    identifier=identifier,
                    age_days=age_days,
                    reason="in_use",
                    size_bytes=stat_result.st_size,
                )
                report.skipped.append(skipped)
            else:
                report.deleted.append(item)
            continue

        if active_guard.is_claimed(target):
            skipped = RetentionItem(
                kind=kind,
                identifier=identifier,
                age_days=age_days,
                reason="in_use",
                size_bytes=stat_result.st_size,
            )
            report.skipped.append(skipped)
            _audit_skipped(skipped)
            continue

        try:
            removed = active_guard.unlink_if_unclaimed(target)
        except OSError as exc:
            message = _error_message(exc)
            report.errors.append((kind, identifier, message))
            _audit_error(kind, identifier, message)
            continue

        if removed:
            report.deleted.append(item)
            _audit_deleted(item)
        elif target.exists():
            skipped = RetentionItem(
                kind=kind,
                identifier=identifier,
                age_days=age_days,
                reason="in_use",
                size_bytes=stat_result.st_size,
            )
            report.skipped.append(skipped)
            _audit_skipped(skipped)
        else:
            skipped = RetentionItem(
                kind=kind,
                identifier=identifier,
                age_days=age_days,
                reason="already_gone",
                size_bytes=stat_result.st_size,
            )
            report.skipped.append(skipped)
            _audit_skipped(skipped)

    report.stale_locks_removed = active_guard.release_stale_locks()
    if emit_summary:
        _emit_summary(report)
    return report


def prune_inbox(
    settings: Settings,
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    guard: ReferenceGuard | None = None,
) -> RetentionReport:
    """Unlink expired inbox attachments, skipping files claimed by a turn."""
    return _prune_files(
        "inbox",
        settings.inbox_dir,
        policy.inbox_days,
        now=now,
        dry_run=dry_run,
        guard=guard,
        emit_summary=True,
    )


def prune_outbox(
    settings: Settings,
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    guard: ReferenceGuard | None = None,
) -> RetentionReport:
    """Remove expired outbox artifacts, skipping symlinks and claimed files."""
    return _prune_files(
        "outbox",
        settings.outbox_dir,
        policy.outbox_days,
        now=now,
        dry_run=dry_run,
        guard=guard,
        emit_summary=True,
    )


@dataclass(frozen=True)
class SessionRecord:
    """One Hermes session row reduced to the fields retention needs."""

    session_id: str
    source: str
    last_active: float | None
    started_at: float | None


class SessionTransport(Protocol):
    """The minimal Hermes session API surface used by the pruner."""

    def list_sessions(self, *, offset: int, limit: int) -> dict[str, Any]: ...

    def delete_session(self, session_id: str) -> int: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Treat redirects as terminal responses instead of following them."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class HttpSessionTransport:
    """A stdlib-only client for the loopback Hermes session API."""

    def __init__(self, base_url: str, api_key: str, *, timeout_s: float = 10.0) -> None:
        self._base_url = str(base_url).rstrip("/")
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._opener = urllib.request.build_opener(_NoRedirect)

    def _request(self, method: str, path: str) -> tuple[int, bytes]:
        # ``Settings`` already restricts the base URL to http(s); the loopback
        # Hermes API is the only endpoint this process contacts.
        request = urllib.request.Request(f"{self._base_url}{path}", method=method)  # noqa: S310
        request.add_header("Authorization", f"Bearer {self._api_key}")
        request.add_header("Accept", "application/json")
        try:
            with self._opener.open(request, timeout=self._timeout_s) as response:
                return int(response.status), response.read()
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HermesUnavailableError(str(exc)) from exc

    def list_sessions(self, *, offset: int, limit: int) -> dict[str, Any]:
        """Return one page of ``api_server`` sessions."""
        query = urllib.parse.urlencode(
            {"source": _API_SERVER_SOURCE, "limit": limit, "offset": offset}
        )
        status, body = self._request("GET", f"/api/sessions?{query}")
        if status != 200:
            raise HermesUnavailableError(f"hermes_sessions_http_{status}")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return payload

    def delete_session(self, session_id: str) -> int:
        """Delete one session and return the HTTP status."""
        quoted = urllib.parse.quote(str(session_id), safe="")
        status, _body = self._request("DELETE", f"/api/sessions/{quoted}")
        return status


def _api_key(settings: Settings) -> str | None:
    secret = settings.hermes_api_key
    if secret is None:
        return None
    try:
        value = secret.get_secret_value().strip()
    except Exception:
        return None
    return value or None


def _as_epoch(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _session_timestamp(row: dict[str, Any]) -> float | None:
    timestamp = _as_epoch(row.get("last_active"))
    if timestamp is None:
        timestamp = _as_epoch(row.get("started_at"))
    return timestamp


def _list_all_sessions(
    transport: SessionTransport,
    report: RetentionReport,
    *,
    dry_run: bool,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    offset = 0
    for _page in range(_MAX_SESSION_PAGES):
        body = transport.list_sessions(offset=offset, limit=_SESSION_PAGE_LIMIT)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            message = "malformed_list_body"
            report.errors.append(("transcript", "-", message))
            if not dry_run:
                _audit_error("transcript", "-", message)
            return records
        records.extend(row for row in data if isinstance(row, dict))
        if not bool(body.get("has_more")):
            return records
        offset += _SESSION_PAGE_LIMIT
    return records


def _prune_sessions(
    settings: Settings,
    policy: RetentionPolicy,
    *,
    now: datetime | None,
    dry_run: bool,
    transport: SessionTransport | None,
    emit_summary: bool,
) -> RetentionReport:
    report = RetentionReport(dry_run=dry_run)
    days = policy.transcript_days
    if days <= 0:
        return report
    api_key = _api_key(settings)
    if api_key is None:
        message = "hermes_api_key_not_configured"
        report.errors.append(("transcript", "-", message))
        if not dry_run:
            _audit_error("transcript", "-", message)
        if emit_summary:
            _emit_summary(report)
        return report

    client = transport or HttpSessionTransport(settings.hermes_base_url, api_key)
    current = _coerce_now(now)
    now_ts = current.timestamp()
    threshold = days * _SECONDS_PER_DAY

    try:
        rows = _list_all_sessions(client, report, dry_run=dry_run)
    except HermesUnavailableError as exc:
        message = str(exc)
        report.errors.append(("transcript", "-", message))
        if not dry_run:
            _audit_error("transcript", "-", message)
        if emit_summary:
            _emit_summary(report)
        return report

    for row in rows:
        source = row.get("source")
        if source != _API_SERVER_SOURCE:
            continue
        session_id = row.get("id")
        if not isinstance(session_id, str) or not session_id:
            message = "missing_session_id"
            report.errors.append(("transcript", "-", message))
            if not dry_run:
                _audit_error("transcript", "-", message)
            continue

        timestamp = _session_timestamp(row)
        if timestamp is None:
            skipped = RetentionItem(
                kind="transcript",
                identifier=session_id,
                age_days=0,
                reason="no_timestamp",
            )
            report.skipped.append(skipped)
            if not dry_run:
                _audit_skipped(skipped)
            continue

        age_seconds = now_ts - timestamp
        age_days = int(age_seconds // _SECONDS_PER_DAY)
        if age_seconds < threshold:
            continue

        item = RetentionItem(
            kind="transcript",
            identifier=session_id,
            age_days=age_days,
            reason="age",
        )
        if dry_run:
            report.deleted.append(item)
            continue

        try:
            status = client.delete_session(session_id)
        except HermesUnavailableError as exc:
            message = str(exc)
            report.errors.append(("transcript", session_id, message))
            _audit_error("transcript", session_id, message)
            break

        if status in (200, 204):
            report.deleted.append(item)
            _audit_deleted(item)
        elif status == 404:
            skipped = RetentionItem(
                kind="transcript",
                identifier=session_id,
                age_days=age_days,
                reason="already_gone",
            )
            report.skipped.append(skipped)
            _audit_skipped(skipped)
        elif status == 409:
            skipped = RetentionItem(
                kind="transcript",
                identifier=session_id,
                age_days=age_days,
                reason="active_turn",
            )
            report.skipped.append(skipped)
            _audit_skipped(skipped)
        else:
            message = f"hermes_delete_http_{status}"
            report.errors.append(("transcript", session_id, message))
            _audit_error("transcript", session_id, message)

    if emit_summary:
        _emit_summary(report)
    return report


def prune_sessions(
    settings: Settings,
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    transport: SessionTransport | None = None,
) -> RetentionReport:
    """Delete expired ``api_server`` sessions through the Hermes HTTP API."""
    return _prune_sessions(
        settings,
        policy,
        now=now,
        dry_run=dry_run,
        transport=transport,
        emit_summary=True,
    )


def _merge(target: RetentionReport, source: RetentionReport) -> None:
    target.deleted.extend(source.deleted)
    target.skipped.extend(source.skipped)
    target.errors.extend(source.errors)
    target.stale_locks_removed += source.stale_locks_removed


def prune_all(
    settings: Settings,
    policy: RetentionPolicy | None = None,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    transport: SessionTransport | None = None,
) -> RetentionReport:
    """Prune inbox, outbox, and transcripts, merging the reports.

    A single ``retention.summary`` audit entry is emitted once at the end.
    """
    active_policy = policy or RetentionPolicy.from_settings(settings)
    merged = RetentionReport(dry_run=dry_run)
    _merge(
        merged,
        _prune_files(
            "inbox",
            settings.inbox_dir,
            active_policy.inbox_days,
            now=now,
            dry_run=dry_run,
            guard=None,
            emit_summary=False,
        ),
    )
    _merge(
        merged,
        _prune_files(
            "outbox",
            settings.outbox_dir,
            active_policy.outbox_days,
            now=now,
            dry_run=dry_run,
            guard=None,
            emit_summary=False,
        ),
    )
    _merge(
        merged,
        _prune_sessions(
            settings,
            active_policy,
            now=now,
            dry_run=dry_run,
            transport=transport,
            emit_summary=False,
        ),
    )
    _emit_summary(merged)
    return merged


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "HermesUnavailableError",
    "HttpSessionTransport",
    "ReferenceGuard",
    "RetentionConfigError",
    "RetentionItem",
    "RetentionPolicy",
    "RetentionReport",
    "SessionRecord",
    "SessionTransport",
    "prune_all",
    "prune_inbox",
    "prune_outbox",
    "prune_sessions",
]
