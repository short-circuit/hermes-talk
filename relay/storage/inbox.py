"""Validated, atomic attachment inbox storage.

The relay is local-only: this module sniffs bytes, confines every path to an
inbox root outside the repository, writes atomically with ``0600`` mode, and
hands the agent a path plus a content hash.  Nothing here makes a network call
and nothing is ever executed.

Security properties implemented here (threat model T-07, T-08, T-09, T-23,
T-31):

* wire-size cap from ``Settings.max_attachment_mb`` with streaming abort;
* magic-byte allowlist only; extensions are never trusted;
* declared extension must belong to the sniffed type's family;
* server-generated filenames and canonical path confinement under the root;
* ``O_CREAT|O_EXCL`` temp file plus ``os.replace`` atomic publication;
* structural parsing (PDF, image, zip) delegated to an isolated child process;
* EXIF/metadata stripping for JPEG, PNG, and WebP (GIF copied unchanged);
* SHA-256 of the stored bytes recorded at ingest and re-verified before use;
* a short ``spoken`` sentence on every rejection;
* audit entries carrying only the basename, hash, MIME type, and size.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from relay.config import Settings
from relay.logging import audit_attachment, path_is_inside_repo
from relay.storage.errors import (
    ArchiveCorruptError,
    ArchiveTooComplexError,
    AttachmentNotFoundError,
    CorruptDocumentError,
    CorruptMediaError,
    DocumentTooComplexError,
    EmptyUploadError,
    EncryptedDocumentError,
    FileTooLargeError,
    ImageTooLargeError,
    InboxConfigError,
    InboxError,
    InboxUnavailableError,
    IntegrityError,
    ParseTimeoutError,
    TypeMismatchError,
    UnsupportedTypeError,
    UploadBusyError,
)
from relay.storage.validation import DEFAULT_LIMITS, ValidationLimits, validate_media

MAX_SNIFF_BYTES = 4096
INLINE_IMAGE_MAX_BYTES = 8 * 1024 * 1024

_STREAM_CHUNK_BYTES = 64 * 1024
_INBOX_DIR_MODE = 0o700
_FILE_MODE = 0o600
_STEM_MAX_CHARS = 64
_FALLBACK_STEM = "attachment"
_ATTACHMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_STRIP_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
_TEXT_WHITESPACE = frozenset("\r\n\t")


@dataclass(frozen=True)
class AllowedType:
    """One allowlisted content type and the extensions that may name it."""

    kind: str
    extensions: tuple[str, ...]


# The allowlist is exact and exhaustive.  HEIC/HEIF is deliberately absent:
# Pillow cannot decode it without an extra plugin, so the phone should convert
# to JPEG before uploading.
ALLOWED_TYPES: Mapping[str, AllowedType] = {
    "image/png": AllowedType(kind="image", extensions=(".png",)),
    "image/jpeg": AllowedType(kind="image", extensions=(".jpg", ".jpeg")),
    "image/webp": AllowedType(kind="image", extensions=(".webp",)),
    "image/gif": AllowedType(kind="image", extensions=(".gif",)),
    "application/pdf": AllowedType(kind="document", extensions=(".pdf",)),
    "application/zip": AllowedType(kind="archive", extensions=(".zip",)),
    "audio/wav": AllowedType(kind="audio", extensions=(".wav",)),
    "audio/mpeg": AllowedType(kind="audio", extensions=(".mp3",)),
    "audio/ogg": AllowedType(kind="audio", extensions=(".ogg", ".oga", ".opus")),
    "audio/flac": AllowedType(kind="audio", extensions=(".flac",)),
    "audio/mp4": AllowedType(kind="audio", extensions=(".m4a", ".mp4")),
    "audio/webm": AllowedType(kind="audio", extensions=(".webm",)),
    "text/plain": AllowedType(kind="text", extensions=(".txt", ".md", ".csv")),
}


class AsyncReadable(Protocol):
    """The minimal async byte source accepted by :meth:`InboxStore.ingest`."""

    async def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True)
class Attachment:
    """One stored attachment; ``path`` is absolute and confined to the inbox."""

    id: str
    path: Path
    mime: str
    kind: str
    sha256: str
    size_bytes: int
    original_name: str
    created_at: str
    detected_mime: str = ""
    uploaded_bytes: int = 0
    facts: dict[str, object] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, object]:
        """Return the client-facing attachment record (no raw bytes)."""
        return {
            "id": self.id,
            "mime": self.mime,
            "kind": self.kind,
            "sha256": self.sha256,
            "content_sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "uploaded_bytes": self.uploaded_bytes,
            "detected_mime": self.detected_mime or self.mime,
            "facts": dict(self.facts),
            "original_name": self.original_name,
            "created_at": self.created_at,
            "path": str(self.path),
        }


def _looks_like_text(head: bytes) -> bool:
    """Return whether a magic-less prefix is plausibly UTF-8 text."""
    if b"\x00" in head:
        return False
    truncated = len(head) >= MAX_SNIFF_BYTES
    max_trim = 3 if truncated else 0
    for trim in range(max_trim + 1):
        candidate = head[: len(head) - trim] if trim else head
        if not candidate:
            return False
        try:
            text = candidate.decode("utf-8").lstrip("\ufeff")
        except UnicodeDecodeError:
            continue
        return all(character.isprintable() or character in _TEXT_WHITESPACE for character in text)
    return False


def sniff_mime(head: bytes) -> str | None:
    """Identify an allowlisted type from magic bytes; ``None`` means reject.

    The extension is never consulted.  Text has no magic, so it is recognized
    last by a strict UTF-8/printability check after known binary signatures
    (including ``MZ`` and ZIP) have been ruled out.
    """
    if not head:
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "audio/webm"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "audio/mp4"
    if head.startswith(b"ID3"):
        return "audio/mpeg"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "audio/mpeg"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "application/zip"
    if head.startswith(b"MZ"):
        return None
    if _looks_like_text(head):
        return "text/plain"
    return None


def sanitize_filename(name: str) -> str:
    """Return a safe stem: basename only, ``[A-Za-z0-9_-]``, bounded.

    Both POSIX and Windows separators are treated as path separators so a
    client-supplied path can never influence where the file lands.  The
    extension is dropped here; :meth:`InboxStore.ingest` appends the canonical
    extension derived from the sniffed type.
    """
    if not isinstance(name, str) or not name.strip():
        return _FALLBACK_STEM
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-_")
    cleaned = cleaned[:_STEM_MAX_CHARS].strip("-_")
    return cleaned or _FALLBACK_STEM


def canonical_extension(mime: str) -> str:
    """Return the canonical stored extension for an allowlisted MIME type."""
    allowed = ALLOWED_TYPES.get(mime)
    if allowed is None:
        return ""
    return allowed.extensions[0]


def extension_matches(mime: str, filename: str) -> bool:
    """Return whether ``filename``'s extension belongs to ``mime``'s family."""
    allowed = ALLOWED_TYPES.get(mime)
    if allowed is None:
        return False
    base = str(filename).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if "." not in base:
        return False
    extension = "." + base.rsplit(".", 1)[1]
    return extension in allowed.extensions


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            msg = "short write to attachment temp file"
            raise OSError(msg)
        view = view[written:]


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_STREAM_CHUNK_BYTES), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class InboxStore:
    """A confined attachment store rooted outside the repository."""

    def __init__(
        self,
        settings: Settings,
        *,
        limits: ValidationLimits | None = None,
    ) -> None:
        inbox_dir = settings.inbox_dir
        if inbox_dir is None:
            msg = "INBOX_DIR is not configured"
            raise InboxConfigError(detail=msg)
        root = Path(inbox_dir).expanduser()
        if path_is_inside_repo(root):
            msg = "INBOX_DIR is inside the repository; attachments must live outside it"
            raise InboxConfigError(detail=msg)
        self._settings = settings
        self._root = root.resolve()
        self._cap_bytes = settings.max_attachment_mb * 1024 * 1024
        self._limits = limits or DEFAULT_LIMITS

    @property
    def root(self) -> Path:
        """The absolute inbox root; it is never inside the repository."""
        return self._root

    @property
    def limits(self) -> ValidationLimits:
        """The structural-parsing caps applied to untrusted media."""
        return self._limits

    def ensure_ready(self) -> None:
        """Create the root with ``0700`` if missing and verify it is writable."""
        root = self._root
        try:
            if not root.exists():
                try:
                    root.mkdir(parents=True, mode=_INBOX_DIR_MODE)
                    os.chmod(root, _INBOX_DIR_MODE)
                except FileExistsError:
                    # Another concurrent upload created the root first.
                    pass
            if not root.is_dir() or not os.access(root, os.W_OK):
                msg = f"inbox root is not a writable directory: {root}"
                raise InboxUnavailableError(detail=msg)
        except InboxError:
            raise
        except OSError as exc:
            msg = f"cannot prepare the inbox root: {root}"
            raise InboxUnavailableError(detail=msg) from exc

    def _assert_inside(self, path: Path) -> None:
        try:
            Path(path).resolve().relative_to(self._root)
        except (OSError, ValueError) as exc:
            msg = "attachment path escaped the inbox root"
            raise InboxUnavailableError(detail=msg) from exc

    def _audit_reject(
        self,
        exc: InboxError,
        *,
        filename: str,
        mime: str | None,
        size_bytes: int,
        sha256: str,
    ) -> None:
        audit_attachment(
            False,
            sha256=sha256,
            mime=mime or "",
            size_bytes=size_bytes,
            reason=exc.code,
            filename=filename,
        )

    async def ingest(
        self,
        *,
        filename: str,
        declared_content_type: str | None,
        stream: AsyncReadable,
    ) -> Attachment:
        """Validate and atomically store one upload, returning its record.

        ``declared_content_type`` is advisory only: the sniffed magic bytes and
        the declared extension decide acceptance.  The stream is consumed in
        bounded chunks and the cap is enforced as bytes arrive.
        """
        stem = sanitize_filename(filename)
        cap = self._cap_bytes
        temp_path = self._root / f".incoming-{uuid.uuid4().hex}.part"
        reencoded_path: Path | None = None
        descriptor: int | None = None
        digest = hashlib.sha256()
        total = 0
        mime: str | None = None
        try:
            self.ensure_ready()
            self._assert_inside(temp_path)
            descriptor = os.open(
                temp_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                _FILE_MODE,
            )

            head = bytearray()
            while len(head) < MAX_SNIFF_BYTES:
                chunk = await stream.read(min(_STREAM_CHUNK_BYTES, MAX_SNIFF_BYTES - len(head)))
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    msg = f"attachment exceeds the {self._settings.max_attachment_mb} MB limit"
                    raise FileTooLargeError(detail=msg)
                head.extend(chunk)
                _write_all(descriptor, chunk)
                digest.update(chunk)

            if total == 0:
                raise EmptyUploadError(detail="the uploaded file contains zero bytes")

            mime = sniff_mime(bytes(head))
            if mime is None:
                raise UnsupportedTypeError(detail="the file's magic bytes are not allowlisted")
            if not extension_matches(mime, filename):
                msg = f"the extension does not belong to the sniffed type {mime}"
                raise TypeMismatchError(detail=msg)

            while True:
                chunk = await stream.read(_STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    msg = f"attachment exceeds the {self._settings.max_attachment_mb} MB limit"
                    raise FileTooLargeError(detail=msg)
                _write_all(descriptor, chunk)
                digest.update(chunk)

            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None

            stored_path = temp_path
            if mime in _STRIP_MIMES:
                reencoded_path = self._root / f".incoming-{uuid.uuid4().hex}.part"
                self._assert_inside(reencoded_path)
                # Reserve the output with 0600; the probe writes into it.
                output_descriptor = os.open(
                    reencoded_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    _FILE_MODE,
                )
                os.close(output_descriptor)

            facts = await validate_media(
                path=temp_path,
                mime=mime,
                limits=self._limits,
                output=reencoded_path,
            )
            if (
                reencoded_path is not None
                and facts.stored_width is not None
                and facts.stored_height is not None
            ):
                stored_path = reencoded_path

            sha256, size_bytes = _hash_file(stored_path)
            attachment_id = f"{stem}-{uuid.uuid4().hex[:12]}{canonical_extension(mime)}"
            final_path = self._root / attachment_id
            self._assert_inside(final_path)
            os.replace(stored_path, final_path)
            normalized_name = filename.replace("\\", "/")

            attachment = Attachment(
                id=attachment_id,
                path=final_path,
                mime=mime,
                kind=ALLOWED_TYPES[mime].kind,
                sha256=sha256,
                size_bytes=size_bytes,
                original_name=f"{stem}{Path(normalized_name).suffix.lower()}",
                created_at=_utcnow_iso(),
                detected_mime=mime,
                uploaded_bytes=total,
                facts=facts.to_metadata(),
            )
            audit_attachment(
                True,
                sha256=attachment.sha256,
                mime=attachment.mime,
                size_bytes=attachment.size_bytes,
                filename=attachment.id,
            )
            return attachment
        except InboxError as exc:
            self._audit_reject(
                exc,
                filename=stem,
                mime=mime,
                size_bytes=total,
                sha256=digest.hexdigest(),
            )
            raise
        except OSError as exc:
            unavailable = InboxUnavailableError(
                detail=f"could not store the attachment: {exc.strerror or exc}",
            )
            self._audit_reject(
                unavailable,
                filename=stem,
                mime=mime,
                size_bytes=total,
                sha256=digest.hexdigest(),
            )
            raise unavailable from exc
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)
            for candidate in (temp_path, reencoded_path):
                if candidate is not None:
                    with suppress(OSError):
                        candidate.unlink()

    def resolve(self, attachment_id: str) -> Path:
        """Map a server-generated id to a confined, existing file path."""
        if (
            not isinstance(attachment_id, str)
            or not _ATTACHMENT_ID_RE.fullmatch(attachment_id)
            or ".." in attachment_id
        ):
            msg = f"invalid attachment id: {attachment_id!r}"
            raise AttachmentNotFoundError(detail=msg)
        candidate = self._root / attachment_id
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self._root)
        except (OSError, ValueError) as exc:
            msg = f"unknown attachment id: {attachment_id!r}"
            raise AttachmentNotFoundError(detail=msg) from exc
        if not resolved.is_file():
            msg = f"attachment is not a regular file: {attachment_id!r}"
            raise AttachmentNotFoundError(detail=msg)
        return resolved

    def delegation_parts(
        self,
        attachments: Sequence[Attachment],
        *,
        inline_images: bool = True,
    ) -> list[dict[str, object]]:
        """Build the ``input`` content array for ``POST /v1/runs``.

        Every attachment gets one stable machine-parsable text line carrying
        its absolute path, hash, MIME type, size, and sanitized name.  Images
        within the inline budget additionally get a data-URL ``image_url``
        part, because Hermes accepts inline images only; documents are handed
        over as filesystem paths.
        """
        parts: list[dict[str, object]] = []
        total = len(attachments)
        for index, attachment in enumerate(attachments, start=1):
            try:
                present = attachment.path.is_file()
            except OSError:
                present = False
            if not present:
                msg = f"attachment file is gone: {attachment.id}"
                raise AttachmentNotFoundError(detail=msg)
            digest = hashlib.sha256()
            try:
                with attachment.path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(_STREAM_CHUNK_BYTES), b""):
                        digest.update(chunk)
            except OSError as exc:
                msg = f"attachment file is unreadable: {attachment.id}"
                raise AttachmentNotFoundError(detail=msg) from exc
            if digest.hexdigest() != attachment.sha256:
                msg = f"stored bytes no longer match the recorded hash: {attachment.id}"
                raise IntegrityError(detail=msg)

            extras = [f"detected_mime={attachment.detected_mime or attachment.mime}"]
            facts = attachment.facts
            pages = facts.get("pages")
            if isinstance(pages, int) and not isinstance(pages, bool):
                extras.append(f"pages={pages}")
            width = facts.get("width")
            height = facts.get("height")
            if (
                isinstance(width, int)
                and not isinstance(width, bool)
                and isinstance(height, int)
                and not isinstance(height, bool)
            ):
                extras.append(f"pixels={width * height}")
            entries = facts.get("entries")
            if isinstance(entries, int) and not isinstance(entries, bool):
                extras.append(f"entries={entries}")

            parts.append(
                {
                    "type": "text",
                    "text": (
                        f"Attached file {index}/{total}: path={attachment.path} "
                        f"sha256={attachment.sha256} mime={attachment.mime} "
                        f"bytes={attachment.size_bytes} name={attachment.original_name} "
                        + " ".join(extras)
                    ),
                }
            )
            if (
                inline_images
                and attachment.kind == "image"
                and attachment.size_bytes <= INLINE_IMAGE_MAX_BYTES
            ):
                try:
                    payload = attachment.path.read_bytes()
                except OSError as exc:
                    msg = f"attachment file is unreadable: {attachment.id}"
                    raise AttachmentNotFoundError(detail=msg) from exc
                encoded = base64.b64encode(payload).decode("ascii")
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{attachment.mime};base64,{encoded}"},
                    }
                )
        return parts


__all__ = [
    "ALLOWED_TYPES",
    "INLINE_IMAGE_MAX_BYTES",
    "MAX_SNIFF_BYTES",
    "AllowedType",
    "ArchiveCorruptError",
    "ArchiveTooComplexError",
    "AsyncReadable",
    "Attachment",
    "AttachmentNotFoundError",
    "CorruptDocumentError",
    "CorruptMediaError",
    "DocumentTooComplexError",
    "EmptyUploadError",
    "EncryptedDocumentError",
    "FileTooLargeError",
    "ImageTooLargeError",
    "InboxConfigError",
    "InboxError",
    "InboxStore",
    "InboxUnavailableError",
    "IntegrityError",
    "ParseTimeoutError",
    "TypeMismatchError",
    "UnsupportedTypeError",
    "UploadBusyError",
    "canonical_extension",
    "extension_matches",
    "sanitize_filename",
    "sniff_mime",
]
