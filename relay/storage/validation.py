"""Parent-side attachment validation.

Structural parsing of untrusted PDF, image, and zip content happens in the
isolated ``_probe`` child process.  This module owns the limits, launches that
child with a wall-clock timeout and a minimal environment, maps its failure
codes onto the spoken error hierarchy, and never decodes media itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import relay.storage._probe as _probe
from relay.storage.errors import (
    ArchiveCorruptError,
    ArchiveTooComplexError,
    CorruptDocumentError,
    CorruptMediaError,
    DocumentTooComplexError,
    EncryptedDocumentError,
    ImageTooLargeError,
    InboxError,
    InboxUnavailableError,
    ParseTimeoutError,
    TypeMismatchError,
)

logger = logging.getLogger(__name__)

_STDERR_TAIL_CHARS = 500
_IMAGE_NORMALIZE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})


@dataclass(frozen=True)
class ValidationLimits:
    """Caps applied to untrusted attachments before delegation."""

    max_archive_entries: int = 512
    max_archive_uncompressed_bytes: int = 256 * 1024 * 1024
    max_archive_ratio: float = 200.0
    max_pdf_pages: int = 200
    max_pdf_text_bytes: int = 2 * 1024 * 1024
    max_image_pixels: int = 40_000_000
    max_image_edge: int = 1024
    parse_timeout_seconds: float = 10.0
    probe_memory_bytes: int = 1024 * 1024 * 1024


DEFAULT_LIMITS = ValidationLimits()


@dataclass(frozen=True)
class ProbeFacts:
    """Raw facts reported by the child process."""

    detected_mime: str = ""
    pages: int | None = None
    text_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    stored_width: int | None = None
    stored_height: int | None = None
    archive_entries: int | None = None
    archive_uncompressed_bytes: int | None = None
    archive_compressed_bytes: int | None = None
    archive_ratio: float | None = None


@dataclass(frozen=True)
class MediaFacts:
    """Validated media facts recorded in the attachment metadata."""

    detected_mime: str = ""
    pages: int | None = None
    text_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    stored_width: int | None = None
    stored_height: int | None = None
    archive_entries: int | None = None
    archive_uncompressed_bytes: int | None = None
    archive_ratio: float | None = None

    def to_metadata(self) -> dict[str, object]:
        """Return only the facts that were actually measured."""
        metadata: dict[str, object] = {}
        if self.detected_mime:
            metadata["detected_mime"] = self.detected_mime
        if self.pages is not None:
            metadata["pages"] = self.pages
        if self.text_bytes is not None:
            metadata["text_bytes"] = self.text_bytes
        if self.width is not None:
            metadata["width"] = self.width
        if self.height is not None:
            metadata["height"] = self.height
        if self.stored_width is not None:
            metadata["stored_width"] = self.stored_width
        if self.stored_height is not None:
            metadata["stored_height"] = self.stored_height
        if self.archive_entries is not None:
            metadata["entries"] = self.archive_entries
        if self.archive_uncompressed_bytes is not None:
            metadata["uncompressed_bytes"] = self.archive_uncompressed_bytes
        if self.archive_ratio is not None:
            metadata["ratio"] = self.archive_ratio
        return metadata


def _corrupt_error(kind: str) -> type[InboxError]:
    if kind == "pdf":
        return CorruptDocumentError
    if kind == "archive":
        return ArchiveCorruptError
    return CorruptMediaError


def _map_reason(reason: str, kind: str, detail: str = "") -> InboxError:
    """Translate a probe failure code into the spoken error hierarchy."""
    if reason in {"too_many_pages", "text_too_large"}:
        error_class: type[InboxError] = DocumentTooComplexError
    elif reason == "encrypted":
        error_class = EncryptedDocumentError
    elif reason == "corrupt":
        error_class = _corrupt_error(kind)
    elif reason == "too_many_pixels":
        error_class = ImageTooLargeError
    elif reason == "decode_failed":
        error_class = CorruptMediaError
    elif reason in {"too_many_entries", "too_much_uncompressed", "ratio_too_high"}:
        error_class = ArchiveTooComplexError
    elif reason == "mime_mismatch":
        error_class = TypeMismatchError
    elif reason == "timeout":
        error_class = ParseTimeoutError
    else:
        error_class = _corrupt_error(kind)
    return error_class(detail=detail or reason)


def _probe_environment() -> dict[str, str]:
    package_parent = Path(__file__).resolve().parents[2]
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(package_parent),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONIOENCODING": "utf-8",
    }


def _request_payload(
    *,
    path: Path,
    mime: str,
    kind: str,
    mode: str,
    output: Path | None,
    limits: ValidationLimits,
) -> bytes:
    request = {
        "kind": kind,
        "path": str(path),
        "output": str(output) if output is not None else None,
        "mime": mime,
        "mode": mode,
        "limits": {
            "max_archive_entries": limits.max_archive_entries,
            "max_archive_uncompressed_bytes": limits.max_archive_uncompressed_bytes,
            "max_archive_ratio": limits.max_archive_ratio,
            "max_pdf_pages": limits.max_pdf_pages,
            "max_pdf_text_bytes": limits.max_pdf_text_bytes,
            "max_image_pixels": limits.max_image_pixels,
            "max_image_edge": limits.max_image_edge,
            "timeout_seconds": limits.parse_timeout_seconds,
            "memory_bytes": limits.probe_memory_bytes,
        },
    }
    return json.dumps(request, separators=(",", ":")).encode("utf-8")


def _as_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _probe_is_available(probe_path: Path) -> bool:
    return probe_path.is_file() and bool(sys.executable)


async def probe_media(
    *,
    path: Path,
    mime: str,
    kind: str,
    mode: str,
    output: Path | None,
    limits: ValidationLimits,
) -> ProbeFacts:
    """Run the isolated probe once and return its validated facts."""
    probe_path = Path(_probe.__file__)
    if not _probe_is_available(probe_path):
        raise InboxUnavailableError(detail="the media probe is unavailable")

    payload = _request_payload(
        path=path,
        mime=mime,
        kind=kind,
        mode=mode,
        output=output,
        limits=limits,
    )
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(probe_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(probe_path.parent),
            env=_probe_environment(),
        )
    except OSError as exc:
        raise InboxUnavailableError(detail="could not start the media probe") from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(payload),
            timeout=limits.parse_timeout_seconds,
        )
    except TimeoutError as exc:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise ParseTimeoutError(
            detail=f"parsing exceeded {limits.parse_timeout_seconds} s",
        ) from exc

    tail = stderr.decode("utf-8", "replace")[-_STDERR_TAIL_CHARS:]
    if tail.strip():
        logger.debug("media probe stderr: %s", tail.strip())

    if process.returncode != 0 or not stdout.strip():
        raise _corrupt_error(kind)("the media probe produced no result")

    try:
        result = json.loads(stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise _corrupt_error(kind)("the media probe produced invalid JSON") from exc

    if not isinstance(result, dict):
        raise _corrupt_error(kind)("the media probe produced a non-object result")

    if not result.get("ok"):
        reason = str(result.get("reason") or "corrupt")
        detail = str(result.get("detail") or reason)
        raise _map_reason(reason, kind, detail)

    detected_mime = str(result.get("detected_mime") or mime)
    return ProbeFacts(
        detected_mime=detected_mime,
        pages=_as_int(result.get("pages")),
        text_bytes=_as_int(result.get("text_bytes")),
        width=_as_int(result.get("width")),
        height=_as_int(result.get("height")),
        stored_width=_as_int(result.get("stored_width")),
        stored_height=_as_int(result.get("stored_height")),
        archive_entries=_as_int(result.get("entries")),
        archive_uncompressed_bytes=_as_int(result.get("uncompressed_bytes")),
        archive_compressed_bytes=_as_int(result.get("compressed_bytes")),
        archive_ratio=_as_float(result.get("ratio")),
    )


def _dispatch(mime: str) -> tuple[str, str] | None:
    if mime == "application/pdf":
        return "pdf", "normalize"
    if mime == "application/zip":
        return "archive", "normalize"
    if mime in _IMAGE_NORMALIZE_MIMES:
        return "image", "normalize"
    if mime == "image/gif":
        return "image", "header"
    return None


async def validate_media(
    *,
    path: Path,
    mime: str,
    limits: ValidationLimits,
    output: Path | None = None,
) -> MediaFacts:
    """Validate one staged attachment by family, spawning the probe as needed."""
    dispatch = _dispatch(mime)
    if dispatch is None:
        return MediaFacts()
    kind, mode = dispatch
    facts = await probe_media(
        path=path,
        mime=mime,
        kind=kind,
        mode=mode,
        output=output if kind == "image" else None,
        limits=limits,
    )
    return MediaFacts(
        detected_mime=facts.detected_mime,
        pages=facts.pages,
        text_bytes=facts.text_bytes,
        width=facts.width,
        height=facts.height,
        stored_width=facts.stored_width,
        stored_height=facts.stored_height,
        archive_entries=facts.archive_entries,
        archive_uncompressed_bytes=facts.archive_uncompressed_bytes,
        archive_ratio=facts.archive_ratio,
    )


__all__ = [
    "DEFAULT_LIMITS",
    "MediaFacts",
    "ProbeFacts",
    "ValidationLimits",
    "probe_media",
    "validate_media",
]
