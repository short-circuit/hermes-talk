"""Hash-at-rest device token store with live rotation and revocation.

The admin CLI is loopback-only by nature: ``python -m relay.tokens`` runs on the
relay host, reads and writes local state, and makes no network calls.  Plaintext
tokens are printed once by the CLI and are never written to disk.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TOKEN_PREFIX = "ht_"  # noqa: S105
TOKEN_BYTES = 32
TOKEN_ID_LENGTH = 8
DEFAULT_STATE_FILENAME = "device_tokens.json"
STATE_VERSION = 1

_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_BEARER_RE = re.compile(r"^\s*Bearer\s+(\S+)\s*$", re.IGNORECASE)
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_ID_RE = re.compile(r"^[0-9a-f]{8}$")
_RECORD_KEYS = frozenset({"token_id", "label", "issued_at", "digest", "sha256", "revoked_at"})
_BOOTSTRAP_ISSUED_AT = ""

_audit = logging.getLogger("relay.audit")
_logger = logging.getLogger(__name__)


class TokenError(Exception):
    """Base class for device token errors."""


class TokenStateError(TokenError):
    """The token state file is corrupt, missing, or unsafe to use."""


class UnknownDeviceError(TokenError):
    """A revoke or rotate target does not exist."""


@dataclass(frozen=True)
class DeviceToken:
    """Public metadata for one device token; never contains token material."""

    token_id: str
    label: str
    issued_at: str
    revoked_at: str | None = None

    @property
    def active(self) -> bool:
        """Return whether the token has not been revoked."""
        return self.revoked_at is None


@dataclass(frozen=True)
class _StoredRecord:
    token: DeviceToken
    digest: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _secret_value(value: Any) -> str:
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        return str(getter())
    return str(value)


def generate_token() -> str:
    """Return a new random device token: ``ht_`` plus 43 base64url characters."""
    raw = secrets.token_bytes(TOKEN_BYTES)
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{TOKEN_PREFIX}{encoded}"


def hash_token(token: str) -> str:
    """Return the lowercase hex SHA-256 digest of ``token``."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_id_for(token: str) -> str:
    """Return the public token id prefix for ``token``."""
    return hash_token(token)[:TOKEN_ID_LENGTH]


def is_well_formed(token: str) -> bool:
    """Cheaply reject strings that cannot be a generated device token."""
    if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
        return False
    payload = token[len(TOKEN_PREFIX) :]
    expected_length = (TOKEN_BYTES * 8 + 5) // 6
    return len(payload) == expected_length and _BASE64URL_RE.fullmatch(payload) is not None


def extract_bearer_token(authorization: str | None) -> str | None:
    """Return the token from a single ``Authorization: Bearer <token>`` value."""
    if not authorization:
        return None
    match = _BEARER_RE.fullmatch(authorization)
    if match is None:
        return None
    return match.group(1)


def log_auth_failure(
    reason: str,
    *,
    client_host: str | None = None,
    path: str | None = None,
) -> None:
    """Emit one structured audit line for an authentication failure."""
    _audit.info(
        "auth.failure",
        extra={
            "event": "auth.failure",
            "reason": reason,
            "client_host": client_host,
            "path": path,
        },
    )


def _inside_git_work_tree(path: Path) -> bool:
    resolved = path.resolve()
    root: Path | None = None
    for parent in [resolved.parent, *resolved.parent.parents]:
        if (parent / ".git").exists():
            root = parent
            break
    if root is None:
        return False
    # The system temp directory is scratch space even when it happens to live
    # inside a work tree; a nested work tree below it is still refused.
    temp_root = Path(tempfile.gettempdir()).resolve()
    return not (root == temp_root or root in temp_root.parents)


def state_path_is_inside_git_work_tree(path: Path | None) -> bool:
    """Return whether ``path`` sits inside a git work tree."""
    return path is not None and _inside_git_work_tree(path)


class TokenStore:
    """A live, hash-at-rest device token store.

    ``path`` may be ``None`` to use bootstrap tokens only.  The store re-stats
    the state file on every verification, so revoke and rotate performed by the
    CLI are visible to a running service without a restart.
    """

    def __init__(
        self,
        path: Path | None,
        *,
        bootstrap_tokens: Sequence[str] = (),
        clock: Callable[[], datetime] | None = None,
        allow_git_work_tree: bool = False,
    ) -> None:
        self._path = path
        self._clock = clock if clock is not None else _utc_now
        self._bootstrap = tuple(_secret_value(token) for token in bootstrap_tokens)
        self._bootstrap_digests = tuple(hash_token(token) for token in self._bootstrap)
        self._records: tuple[_StoredRecord, ...] = ()
        self._state_stat: tuple[int, int, int] | None = None
        self._state_error: TokenStateError | None = None
        self._bootstrap_warned = False
        if state_path_is_inside_git_work_tree(path) and not allow_git_work_tree:
            msg = f"token state path is inside a git work tree: {path}"
            raise TokenStateError(msg)
        self._refresh(strict=False)

    @property
    def bootstrap_token_count(self) -> int:
        """Number of deprecated bootstrap tokens configured."""
        return len(self._bootstrap)

    def state_path_inside_git_work_tree(self) -> bool:
        """Return whether this store's state path sits inside a git work tree."""
        return state_path_is_inside_git_work_tree(self._path)

    def state_path_display(self) -> str:
        """Render the state path relative to the working directory when possible."""
        if self._path is None:
            return "bootstrap-only"
        try:
            return str(self._path.relative_to(Path.cwd()))
        except ValueError:
            return str(self._path)

    def has_configured_tokens(self) -> bool:
        """Return whether any active state record or bootstrap token exists."""
        self._refresh(strict=False)
        if self._state_error is not None:
            return True
        return bool(self._bootstrap) or any(record.token.active for record in self._records)

    def reload(self) -> None:
        """Force a re-read of the state file."""
        self._state_stat = None
        self._state_error = None
        self._refresh(strict=True)

    def active_tokens(self) -> tuple[DeviceToken, ...]:
        """Return active device records only; plaintext is never included."""
        self._refresh(strict=False)
        return tuple(record.token for record in self._records if record.token.active)

    def all_tokens(self) -> tuple[DeviceToken, ...]:
        """Return every device record, including revoked predecessors."""
        self._refresh(strict=False)
        if self._state_error is not None:
            raise self._state_error
        return tuple(record.token for record in self._records)

    def issue(self, label: str) -> tuple[str, DeviceToken]:
        """Create a new device token, persist its digest, and return the plaintext once."""

        def mutate(records: list[_StoredRecord]) -> tuple[list[_StoredRecord], Any]:
            existing = {record.token.token_id for record in records}
            while True:
                token = generate_token()
                digest = hash_token(token)
                token_id = digest[:TOKEN_ID_LENGTH]
                if token_id not in existing:
                    break
            record = DeviceToken(
                token_id=token_id,
                label=label,
                issued_at=self._now_iso(),
            )
            return [*records, _StoredRecord(token=record, digest=digest)], (token, record)

        token, record = self._mutate(mutate)
        self._audit_event("auth.issue", label=record.label, token_id=record.token_id)
        return token, record

    def revoke(
        self,
        *,
        label: str | None = None,
        token_id: str | None = None,
    ) -> DeviceToken:
        """Revoke a device token and return its metadata.

        ``revoke(label=...)`` targets the most recently issued active record for
        that label when one exists; if every matching record is already revoked,
        it returns an already-revoked match unchanged (idempotent).
        ``revoke(token_id=...)`` of an already revoked id is likewise idempotent.
        """

        def mutate(records: list[_StoredRecord]) -> tuple[list[_StoredRecord], Any]:
            target = self._select(records, label=label, token_id=token_id)
            if target.token.revoked_at is not None:
                return records, target.token
            revoked_at = self._now_iso()
            updated = [
                _StoredRecord(token=replace(item.token, revoked_at=revoked_at), digest=item.digest)
                if item is target
                else item
                for item in records
            ]
            return updated, replace(target.token, revoked_at=revoked_at)

        record = self._mutate(mutate)
        self._audit_event("auth.revoke", label=record.label, token_id=record.token_id)
        return record

    def rotate(
        self,
        *,
        label: str | None = None,
        token_id: str | None = None,
    ) -> tuple[str, DeviceToken]:
        """Revoke the predecessor and issue a successor in one atomic write."""

        def mutate(records: list[_StoredRecord]) -> tuple[list[_StoredRecord], Any]:
            targets = self._select_many(records, label=label, token_id=token_id, active_only=True)
            predecessor_ids = [target.token.token_id for target in targets]
            revoked_at = self._now_iso()
            target_ids = set(predecessor_ids)
            updated = [
                _StoredRecord(token=replace(item.token, revoked_at=revoked_at), digest=item.digest)
                if item.token.token_id in target_ids
                else item
                for item in records
            ]
            existing = {record.token.token_id for record in updated}
            while True:
                token = generate_token()
                digest = hash_token(token)
                new_token_id = digest[:TOKEN_ID_LENGTH]
                if new_token_id not in existing:
                    break
            record = DeviceToken(
                token_id=new_token_id,
                label=targets[0].token.label,
                issued_at=revoked_at,
            )
            updated.append(_StoredRecord(token=record, digest=digest))
            return updated, (token, record, predecessor_ids)

        token, record, predecessor_ids = self._mutate(mutate)
        self._audit_event(
            "auth.rotate",
            label=record.label,
            token_id=record.token_id,
            predecessor_token_id=",".join(predecessor_ids),
        )
        return token, record

    def verify(
        self,
        token: str,
        *,
        client_host: str | None = None,
        path: str | None = None,
    ) -> DeviceToken | None:
        """Verify a bearer token in constant time, reloading state on every call."""
        self._refresh(strict=False)
        if self._state_error is not None:
            _audit.error(
                "auth.state_error",
                extra={
                    "event": "auth.state_error",
                    "state_path": self.state_path_display(),
                    "detail": str(self._state_error),
                },
            )
            return None

        active = tuple(record for record in self._records if record.token.active)
        if not isinstance(token, str):
            log_auth_failure("malformed_token", client_host=client_host, path=path)
            return None
        well_formed = is_well_formed(token)

        if not well_formed:
            if not active and self._bootstrap:
                matched = self._match_bootstrap(hash_token(token))
                if matched is not None:
                    return self._bootstrap_record(matched)
            log_auth_failure("malformed_token", client_host=client_host, path=path)
            return None

        digest = hash_token(token)
        matched_record: _StoredRecord | None = None
        for record in active:
            equal = hmac.compare_digest(digest, record.digest)
            if equal and matched_record is None:
                matched_record = record
        if matched_record is not None:
            return matched_record.token

        if not active:
            if not self._bootstrap:
                log_auth_failure("no_tokens_configured", client_host=client_host, path=path)
                return None
            matched = self._match_bootstrap(digest)
            if matched is not None:
                return self._bootstrap_record(matched)

        log_auth_failure("unknown_token", client_host=client_host, path=path)
        return None

    def _match_bootstrap(self, digest: str) -> int | None:
        matched: int | None = None
        for index, bootstrap_digest in enumerate(self._bootstrap_digests):
            equal = hmac.compare_digest(digest, bootstrap_digest)
            if equal and matched is None:
                matched = index
        return matched

    def _bootstrap_record(self, index: int) -> DeviceToken:
        if not self._bootstrap_warned:
            self._bootstrap_warned = True
            _logger.warning(
                "auth.bootstrap_tokens_active",
                extra={"event": "auth.bootstrap_tokens_active"},
            )
        bootstrap_digest = self._bootstrap_digests[index]
        return DeviceToken(
            token_id=bootstrap_digest[:TOKEN_ID_LENGTH],
            label="bootstrap",
            issued_at=_BOOTSTRAP_ISSUED_AT,
        )

    def _now_iso(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()

    def _audit_event(self, event: str, **fields: Any) -> None:
        _audit.info(event, extra={"event": event, **fields})

    def _select(
        self,
        records: list[_StoredRecord],
        *,
        label: str | None,
        token_id: str | None,
    ) -> _StoredRecord:
        matches = self._select_many(records, label=label, token_id=token_id, active_only=False)
        active = [record for record in matches if record.token.active]
        # Active matches win so a revoke-by-label cannot miss the post-rotation successor.
        return active[-1] if active else matches[0]

    def _select_many(
        self,
        records: list[_StoredRecord],
        *,
        label: str | None,
        token_id: str | None,
        active_only: bool,
    ) -> list[_StoredRecord]:
        if label is None and token_id is None:
            msg = "specify --label or --id"
            raise TokenError(msg)
        matches = [
            record
            for record in records
            if (label is None or record.token.label == label)
            and (token_id is None or record.token.token_id == token_id)
            and (not active_only or record.token.active)
        ]
        if not matches:
            target = f"label={label!r}" if label is not None else f"token_id={token_id!r}"
            msg = f"no device token found for {target}"
            raise UnknownDeviceError(msg)
        return matches

    def _mutate(
        self,
        mutate: Callable[[list[_StoredRecord]], tuple[list[_StoredRecord], Any]],
    ) -> Any:
        if self._path is None:
            msg = "no token state path configured; cannot persist changes"
            raise TokenStateError(msg)
        self._ensure_parent()
        lock_path = self._path.with_name(self._path.name + ".lock")
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            records = list(self._read_records_from_disk())
            new_records, result = mutate(records)
            self._atomic_write(tuple(new_records))
            self._records = tuple(new_records)
            self._state_stat = self._stat_signature()
            self._state_error = None
            return result
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def _ensure_parent(self) -> None:
        parent = self._path.parent if self._path is not None else None
        if parent is None or parent.exists():
            return
        parent.mkdir(mode=0o700, parents=True)
        with suppress(OSError):
            parent.chmod(0o700)

    def _stat_signature(self) -> tuple[int, int, int] | None:
        if self._path is None:
            return None
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            msg = f"cannot stat token state file: {exc}"
            raise TokenStateError(msg) from exc
        return (stat.st_mtime_ns, stat.st_size, stat.st_ino)

    def _refresh(self, *, strict: bool) -> None:
        if self._path is None:
            self._records = ()
            self._state_stat = None
            self._state_error = None
            return
        try:
            signature = self._stat_signature()
        except TokenStateError as exc:
            self._records = ()
            self._state_error = exc
            if strict:
                raise
            return
        if signature is None:
            self._records = ()
            self._state_stat = None
            self._state_error = None
            return
        if signature == self._state_stat:
            return
        try:
            records = self._read_records_from_disk()
        except TokenStateError as exc:
            self._records = ()
            self._state_stat = signature
            self._state_error = exc
            if strict:
                raise
            return
        self._records = records
        self._state_stat = signature
        self._state_error = None

    def _read_records_from_disk(self) -> tuple[_StoredRecord, ...]:
        path = self._path
        if path is None:
            return ()
        try:
            stat = path.stat()
        except FileNotFoundError:
            return ()
        except OSError as exc:
            msg = f"cannot stat token state file: {exc}"
            raise TokenStateError(msg) from exc
        mode = stat.st_mode & 0o777
        if mode & 0o077:
            try:
                os.chmod(path, 0o600)
            except OSError as exc:
                _logger.warning(
                    "auth.state_mode_repair_failed",
                    extra={
                        "event": "auth.state_mode_repair_failed",
                        "state_path": self.state_path_display(),
                        "detail": str(exc),
                    },
                )
            else:
                _logger.warning(
                    "auth.state_mode_repaired",
                    extra={
                        "event": "auth.state_mode_repaired",
                        "state_path": self.state_path_display(),
                        "previous_mode": oct(mode),
                    },
                )
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        except OSError as exc:
            msg = f"cannot read token state file: {exc}"
            raise TokenStateError(msg) from exc
        return self._parse_state(raw)

    def _parse_state(self, raw: str) -> tuple[_StoredRecord, ...]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            msg = f"token state file is not valid JSON: {self.state_path_display()}"
            raise TokenStateError(msg) from exc
        if not isinstance(payload, dict):
            msg = "token state must be a JSON object"
            raise TokenStateError(msg)
        if payload.get("version") != STATE_VERSION:
            msg = f"unsupported token state version: {payload.get('version')!r}"
            raise TokenStateError(msg)
        entries = payload.get("tokens", payload.get("devices"))
        if not isinstance(entries, list):
            msg = "token state must contain a list of device records"
            raise TokenStateError(msg)
        seen: set[str] = set()
        records: list[_StoredRecord] = []
        for entry in entries:
            if not isinstance(entry, dict):
                msg = "each device record must be a JSON object"
                raise TokenStateError(msg)
            unexpected = set(entry) - _RECORD_KEYS
            if unexpected:
                msg = f"unexpected device record fields: {sorted(unexpected)}"
                raise TokenStateError(msg)
            token_id = entry.get("token_id")
            label = entry.get("label")
            issued_at = entry.get("issued_at")
            digest = entry.get("digest", entry.get("sha256"))
            revoked_at = entry.get("revoked_at")
            if not isinstance(token_id, str) or _TOKEN_ID_RE.fullmatch(token_id) is None:
                msg = "device record token_id must be 8 lowercase hex characters"
                raise TokenStateError(msg)
            if not isinstance(label, str):
                msg = "device record label must be a string"
                raise TokenStateError(msg)
            if not isinstance(issued_at, str):
                msg = "device record issued_at must be an ISO-8601 string"
                raise TokenStateError(msg)
            self._validate_iso(issued_at, "issued_at")
            if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
                msg = "device record digest must be 64 lowercase hex characters"
                raise TokenStateError(msg)
            if revoked_at is not None:
                if not isinstance(revoked_at, str):
                    msg = "device record revoked_at must be null or an ISO-8601 string"
                    raise TokenStateError(msg)
                self._validate_iso(revoked_at, "revoked_at")
            if token_id in seen:
                msg = f"duplicate device token_id: {token_id}"
                raise TokenStateError(msg)
            seen.add(token_id)
            records.append(
                _StoredRecord(
                    token=DeviceToken(
                        token_id=token_id,
                        label=label,
                        issued_at=issued_at,
                        revoked_at=revoked_at,
                    ),
                    digest=digest,
                )
            )
        return tuple(records)

    @staticmethod
    def _validate_iso(value: str, field: str) -> None:
        try:
            datetime.fromisoformat(value)
        except ValueError as exc:
            msg = f"device record {field} is not a valid ISO-8601 timestamp"
            raise TokenStateError(msg) from exc

    def _atomic_write(self, records: tuple[_StoredRecord, ...]) -> None:
        path = self._path
        if path is None:
            msg = "no token state path configured"
            raise TokenStateError(msg)
        payload = {
            "version": STATE_VERSION,
            "tokens": [
                {
                    "token_id": record.token.token_id,
                    "label": record.token.label,
                    "issued_at": record.token.issued_at,
                    "digest": record.digest,
                    "revoked_at": record.token.revoked_at,
                }
                for record in records
            ],
        }
        data = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
        tmp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp_path, path)
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception as exc:
            msg = f"failed to write token state: {exc}"
            raise TokenStateError(msg) from exc
        finally:
            with suppress(OSError):
                tmp_path.unlink()


class _UsageError(Exception):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="python -m relay.tokens",
        description="Manage hash-at-rest device tokens on the relay host.",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=None,
        help="token state file (default: $DATA_DIR/device_tokens.json)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    issue_parser = subparsers.add_parser("issue", help="issue a new device token")
    issue_parser.add_argument("--label", required=True, help="device label")

    revoke_parser = subparsers.add_parser("revoke", help="revoke a device token")
    revoke_group = revoke_parser.add_mutually_exclusive_group(required=True)
    revoke_group.add_argument("--label", help="device label")
    revoke_group.add_argument("--id", dest="token_id", help="token id prefix")

    rotate_parser = subparsers.add_parser("rotate", help="rotate a device token")
    rotate_group = rotate_parser.add_mutually_exclusive_group(required=True)
    rotate_group.add_argument("--label", help="device label")
    rotate_group.add_argument("--id", dest="token_id", help="token id prefix")

    subparsers.add_parser("list", help="list device records")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the loopback-only token administration CLI."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    from relay.config import Settings
    from relay.logging import configure_logging

    settings = Settings(_env_file=None)
    configure_logging(settings)
    for handler in logging.getLogger().handlers:
        if (
            getattr(handler, "_relay_managed", False)
            and getattr(handler, "_relay_kind", "stream") == "stream"
            and isinstance(handler, logging.StreamHandler)
        ):
            handler.setStream(sys.stderr)

    state_path = (
        args.state_path if args.state_path is not None else settings.device_token_state_path()
    )
    if state_path is None:
        print(
            "error: DATA_DIR is unset and no --state-path was given; "
            "set DATA_DIR or pass --state-path PATH",
            file=sys.stderr,
        )
        return 1

    try:
        store = TokenStore(state_path)
    except TokenStateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        if args.command == "issue":
            token, record = store.issue(args.label)
            print(token)
            print(
                f"issued label={record.label} token_id={record.token_id} "
                f"state={store.state_path_display()}"
            )
        elif args.command == "revoke":
            record = store.revoke(label=args.label, token_id=args.token_id)
            print(f"revoked label={record.label} token_id={record.token_id}")
        elif args.command == "rotate":
            predecessor_ids = [
                record.token_id
                for record in store.active_tokens()
                if (args.label is None or record.label == args.label)
                and (args.token_id is None or record.token_id == args.token_id)
            ]
            token, record = store.rotate(label=args.label, token_id=args.token_id)
            print(token)
            print(
                f"rotated label={record.label} token_id={record.token_id} "
                f"revoked={','.join(predecessor_ids)} state={store.state_path_display()}"
            )
        elif args.command == "list":
            for record in store.all_tokens():
                status = "active" if record.active else "revoked"
                print(f"{record.token_id}\t{record.label}\t{record.issued_at}\t{status}")
    except TokenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
