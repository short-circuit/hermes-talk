"""Confined, read-only artifact store for agent-produced files.

The voice agent writes generated images, documents, audio, and text into
``OUTBOX_DIR``; this module lists and opens those files for the authenticated
phone.  It performs local filesystem reads only and never makes a network call.

Confinement and expiry rules are explicit:

* an artifact id is a relative POSIX path under the outbox root; absolute
  paths, backslashes, control characters, empty/dot segments, and hidden
  segments are rejected;
* resolution must land exactly on the candidate path, so a symlink anywhere in
  the chain is rejected rather than followed;
* the final component is opened with ``O_NOFOLLOW`` and must be a regular file;
* dot-prefixed entries (including the pruner's ``.locks`` directory) are never
  listed, and this module never creates or deletes anything;
* when retention is enabled, an artifact whose age reaches the retention
  window is treated as expired and is neither listed nor served.

Audit entries carry only the relative artifact id, SHA-256, MIME type, byte
size, and a stable reason code; absolute paths never reach the log.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from relay.config import Settings
from relay.logging import path_is_inside_repo
from relay.storage.inbox import sniff_mime

_ARTIFACT_ID_MAX_CHARS = 1024
_SEGMENT_MAX_CHARS = 255
_MAX_SCAN_ENTRIES = 10000
_STREAM_CHUNK_BYTES = 64 * 1024
_MAX_HEAD_BYTES = 4096
_SECONDS_PER_DAY = 86400
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200

_EXTENSION_MIMES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".html": "text/html",
    ".svg": "image/svg+xml",
    ".zip": "application/zip",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}


class OutboxError(Exception):
    """Base class for an artifact decision that maps to an HTTP error."""

    status_code: int = 500
    code: str = "outbox_error"
    spoken: str = "Something went wrong opening that file. Try again."
    detail: str = "artifact rejected"
    retry_after: int | None = None

    def __init__(
        self,
        *,
        detail: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        if detail is not None:
            self.detail = detail
        self.retry_after = retry_after
        super().__init__(self.detail)

    def to_body(self, correlation_id: str | None = None) -> dict[str, object]:
        """Render the client-facing error body; never contains a path."""
        body: dict[str, object] = {
            "error": self.code,
            "detail": self.detail,
            "spoken": self.spoken,
            "retry_after": self.retry_after,
        }
        if correlation_id:
            body["correlation_id"] = correlation_id
        return body

    def headers(self) -> dict[str, str]:
        """Return HTTP headers, including ``Retry-After`` when known."""
        if self.retry_after is None:
            return {}
        return {"Retry-After": str(self.retry_after)}


class OutboxConfigError(OutboxError):
    """The outbox root is unset or lives inside the repository."""

    status_code = 503
    code = "outbox_not_configured"
    spoken = "Files are not set up on this relay yet."
    detail = "outbox_dir is not configured"


class OutboxUnavailableError(OutboxError):
    """The outbox root cannot be read or an artifact cannot be opened."""

    status_code = 503
    code = "outbox_unavailable"
    spoken = "The relay cannot open that file right now."
    detail = "the artifact outbox is unavailable"


class ArtifactNotFoundError(OutboxError):
    """An artifact id is unknown, hidden, non-regular, or escapes the root."""

    status_code = 404
    code = "artifact_not_found"
    spoken = "That file could not be found."
    detail = "the artifact does not exist"


class ArtifactExpiredError(ArtifactNotFoundError):
    """The artifact is older than the configured retention window."""

    code = "artifact_expired"
    spoken = "That file has expired."
    detail = "the artifact has expired"


@dataclass(frozen=True)
class Artifact:
    """One artifact described by a relative id; no absolute path is exposed."""

    id: str
    name: str
    mime: str
    size_bytes: int
    sha256: str
    created_at: str
    expires_at: str | None
    mtime_ns: int

    def to_public_dict(self) -> dict[str, object]:
        """Return the client-facing artifact record without any absolute path."""
        return {
            "id": self.id,
            "name": self.name,
            "mime": self.mime,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


class ArtifactHandle:
    """An open read-only descriptor paired with its :class:`Artifact`.

    The caller owns the descriptor and must close it; :meth:`close` is
    idempotent and swallows ``OSError`` so cleanup in a ``finally`` block cannot
    mask the original response.
    """

    def __init__(self, artifact: Artifact, descriptor: int) -> None:
        self.artifact = artifact
        self._descriptor = descriptor

    @property
    def fd(self) -> int:
        """The open descriptor, or ``-1`` after :meth:`close`."""
        return self._descriptor

    def read_chunk(self, size: int = _STREAM_CHUNK_BYTES) -> bytes:
        """Read at most ``size`` bytes; return ``b""`` at end of file or close."""
        if self._descriptor < 0:
            return b""
        return os.read(self._descriptor, size)

    def close(self) -> None:
        """Close the descriptor once; repeated calls are harmless."""
        descriptor = self._descriptor
        if descriptor < 0:
            return
        self._descriptor = -1
        with suppress(OSError):
            os.close(descriptor)


def _iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def _segments(artifact_id: object) -> tuple[str, ...]:
    """Validate an artifact id and return its path segments.

    Every failure is reported as :class:`ArtifactNotFoundError` so the client
    cannot learn which specific check rejected the id.
    """
    if not isinstance(artifact_id, str):
        msg = "artifact id must be a string"
        raise ArtifactNotFoundError(detail=msg)
    if not artifact_id or len(artifact_id) > _ARTIFACT_ID_MAX_CHARS:
        msg = "artifact id is empty or too long"
        raise ArtifactNotFoundError(detail=msg)
    if artifact_id.startswith("/") or "\\" in artifact_id:
        msg = "artifact id must be a relative POSIX path"
        raise ArtifactNotFoundError(detail=msg)
    if any(ord(character) < 32 for character in artifact_id):
        msg = "artifact id contains a control character"
        raise ArtifactNotFoundError(detail=msg)
    parts = artifact_id.split("/")
    for part in parts:
        if not part or part in {".", ".."} or part.startswith("."):
            msg = "artifact id contains an invalid segment"
            raise ArtifactNotFoundError(detail=msg)
        if len(part) > _SEGMENT_MAX_CHARS:
            msg = "artifact id contains an overlong segment"
            raise ArtifactNotFoundError(detail=msg)
    return tuple(parts)


def _hash_fd(descriptor: int) -> str:
    """Return the SHA-256 of the descriptor contents, rewinding first."""
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, _STREAM_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def mime_for(name: str, head: bytes) -> str:
    """Choose a MIME type from the extension, then magic bytes, then a default.

    Extension-first is deliberate: artifacts are produced by the agent rather
    than uploaded by an untrusted client, and responses are always sent with
    ``Content-Disposition: attachment`` plus ``X-Content-Type-Options: nosniff``.
    """
    suffix = Path(name).suffix.lower()
    curated = _EXTENSION_MIMES.get(suffix)
    if curated is not None:
        return curated
    sniffed = sniff_mime(head)
    if sniffed is not None:
        return sniffed
    return "application/octet-stream"


def _is_not_found_errno(value: int | None) -> bool:
    return value in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR}


class OutboxStore:
    """A confined, read-only store rooted outside the repository."""

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Callable[[], float] | None = None,
        digest_cache_size: int = 256,
    ) -> None:
        outbox_dir = settings.outbox_dir
        if outbox_dir is None:
            msg = "OUTBOX_DIR is not configured"
            raise OutboxConfigError(detail=msg)
        root = Path(outbox_dir).expanduser()
        if path_is_inside_repo(root):
            msg = "OUTBOX_DIR is inside the repository; artifacts must live outside it"
            raise OutboxConfigError(detail=msg)
        self._root = root.resolve()
        self._retention_days = max(int(settings.outbox_retention_days), 0)
        self._clock = clock if clock is not None else time.time
        self._cache_size = max(int(digest_cache_size), 0)
        self._digest_cache: OrderedDict[tuple[str, int, int], str] = OrderedDict()
        self._cache_lock = threading.Lock()

    @property
    def root(self) -> Path:
        """The resolved outbox root; never inside the repository."""
        return self._root

    @property
    def retention_days(self) -> int:
        """The configured retention window in days; ``0`` disables expiry."""
        return self._retention_days

    @property
    def available(self) -> bool:
        """Return whether the root exists and is a readable directory."""
        try:
            return self._root.is_dir() and os.access(self._root, os.R_OK | os.X_OK)
        except OSError:
            return False

    def _is_expired(self, mtime: float) -> bool:
        if self._retention_days <= 0:
            return False
        age = self._clock() - mtime
        return age >= self._retention_days * _SECONDS_PER_DAY

    def _expires_at(self, mtime: float) -> str | None:
        if self._retention_days <= 0:
            return None
        return _iso_utc(mtime + self._retention_days * _SECONDS_PER_DAY)

    def _cached_digest(
        self,
        artifact_id: str,
        *,
        mtime_ns: int,
        size_bytes: int,
        descriptor: int,
    ) -> str:
        key = (artifact_id, mtime_ns, size_bytes)
        with self._cache_lock:
            cached = self._digest_cache.get(key)
            if cached is not None:
                self._digest_cache.move_to_end(key)
                return cached
        digest = _hash_fd(descriptor)
        with self._cache_lock:
            if self._cache_size > 0:
                self._digest_cache[key] = digest
                self._digest_cache.move_to_end(key)
                while len(self._digest_cache) > self._cache_size:
                    self._digest_cache.popitem(last=False)
        return digest

    def _resolve(self, artifact_id: str) -> tuple[Path, tuple[str, ...]]:
        segments = _segments(artifact_id)
        candidate = self._root.joinpath(*segments)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            if _is_not_found_errno(exc.errno):
                msg = "the artifact does not exist"
                raise ArtifactNotFoundError(detail=msg) from exc
            msg = "the artifact could not be resolved"
            raise OutboxUnavailableError(detail=msg) from exc
        if resolved != candidate or not resolved.is_relative_to(self._root):
            msg = "the artifact path escaped the outbox root"
            raise ArtifactNotFoundError(detail=msg)
        return resolved, segments

    def open_artifact(self, artifact_id: str) -> ArtifactHandle:
        """Open one artifact for reading, enforcing confinement and expiry."""
        resolved, segments = self._resolve(artifact_id)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(resolved, flags)
        except OSError as exc:
            if _is_not_found_errno(exc.errno):
                msg = "the artifact does not exist"
                raise ArtifactNotFoundError(detail=msg) from exc
            msg = "the artifact could not be opened"
            raise OutboxUnavailableError(detail=msg) from exc

        try:
            stat_result = os.fstat(descriptor)
            if not stat.S_ISREG(stat_result.st_mode):
                msg = "the artifact is not a regular file"
                raise ArtifactNotFoundError(detail=msg)
            relative_id = "/".join(segments)
            digest = self._cached_digest(
                relative_id,
                mtime_ns=stat_result.st_mtime_ns,
                size_bytes=stat_result.st_size,
                descriptor=descriptor,
            )
            head = os.pread(descriptor, _MAX_HEAD_BYTES, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            if self._is_expired(stat_result.st_mtime):
                raise ArtifactExpiredError()
            artifact = Artifact(
                id=relative_id,
                name=resolved.name,
                mime=mime_for(resolved.name, head),
                size_bytes=stat_result.st_size,
                sha256=digest,
                created_at=_iso_utc(stat_result.st_mtime),
                expires_at=self._expires_at(stat_result.st_mtime),
                mtime_ns=stat_result.st_mtime_ns,
            )
            return ArtifactHandle(artifact, descriptor)
        except OutboxError:
            with suppress(OSError):
                os.close(descriptor)
            raise
        except OSError as exc:
            with suppress(OSError):
                os.close(descriptor)
            msg = "the artifact could not be read"
            raise OutboxUnavailableError(detail=msg) from exc
        except Exception:
            with suppress(OSError):
                os.close(descriptor)
            raise

    def _scan(self) -> list[Path]:
        """Yield regular non-hidden files under the root, capped at 10 000."""
        files: list[Path] = []
        seen = 0
        stack = [self._root]
        while stack:
            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in entries:
                seen += 1
                if seen > _MAX_SCAN_ENTRIES:
                    return files
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        files.append(Path(entry.path))
                except OSError:
                    continue
        return files

    def _artifact_for_path(self, path: Path) -> Artifact | None:
        relative_id = path.relative_to(self._root).as_posix()
        descriptor = -1
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            stat_result = os.fstat(descriptor)
            if not stat.S_ISREG(stat_result.st_mode):
                return None
            if self._is_expired(stat_result.st_mtime):
                return None
            digest = self._cached_digest(
                relative_id,
                mtime_ns=stat_result.st_mtime_ns,
                size_bytes=stat_result.st_size,
                descriptor=descriptor,
            )
            head = os.pread(descriptor, _MAX_HEAD_BYTES, 0)
            return Artifact(
                id=relative_id,
                name=path.name,
                mime=mime_for(path.name, head),
                size_bytes=stat_result.st_size,
                sha256=digest,
                created_at=_iso_utc(stat_result.st_mtime),
                expires_at=self._expires_at(stat_result.st_mtime),
                mtime_ns=stat_result.st_mtime_ns,
            )
        except OSError:
            return None
        finally:
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)

    def list_artifacts(self, *, limit: int = _DEFAULT_LIMIT) -> list[Artifact]:
        """List unexpired artifacts newest first, without following symlinks."""
        try:
            if not self._root.exists():
                return []
            if not self._root.is_dir():
                msg = "the outbox root is not a directory"
                raise OutboxUnavailableError(detail=msg)
            with os.scandir(self._root) as entries:
                for _entry in entries:
                    break
        except OutboxError:
            raise
        except OSError as exc:
            msg = "the outbox root could not be read"
            raise OutboxUnavailableError(detail=msg) from exc

        artifacts: list[Artifact] = []
        for path in self._scan():
            artifact = self._artifact_for_path(path)
            if artifact is not None:
                artifacts.append(artifact)
        artifacts.sort(key=lambda item: (-item.mtime_ns, item.id))
        clamped = max(1, min(_MAX_LIMIT, int(limit)))
        return artifacts[:clamped]


__all__ = [
    "Artifact",
    "ArtifactExpiredError",
    "ArtifactHandle",
    "ArtifactNotFoundError",
    "OutboxConfigError",
    "OutboxError",
    "OutboxStore",
    "OutboxUnavailableError",
    "mime_for",
]
