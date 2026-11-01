"""Self-contained child-process media probe.

The relay runs this file as a separate interpreter::

    python _probe.py < request.json

It reads exactly one JSON request from stdin and writes exactly one JSON object
to stdout.  Nothing here imports from ``relay``; the only third-party imports
are Pillow and pypdf, and they are loaded lazily inside the relevant branch.

Every failure is reported as ``{"ok": false, "reason": "<code>",
"detail": "<short, path-free text>"}``.  The process never raises: even
``MemoryError`` is converted into a failure object.
"""

from __future__ import annotations

import json
import os
import sys
import time
import zipfile

_STREAM_CHUNK_BYTES = 64 * 1024
_JPEG_QUALITY = 90
_WEBP_QUALITY = 90
_FORMAT_TO_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}
_MIME_TO_FORMAT = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}


def _emit(payload: dict[str, object]) -> None:
    """Write one compact JSON object followed by a newline."""
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _failure(reason: str, detail: str) -> dict[str, object]:
    return {"ok": False, "reason": reason, "detail": detail}


def _apply_rlimits(limits: dict[str, object]) -> None:
    """Bound address space and CPU before opening any input."""
    try:
        import resource
    except ImportError:
        return

    try:
        memory_bytes = int(limits.get("memory_bytes", 0) or 0)
        cpu_seconds = int(limits.get("timeout_seconds", 0) or 0) + 2
    except (TypeError, ValueError):
        return

    for name, value in (
        ("RLIMIT_AS", memory_bytes),
        ("RLIMIT_CPU", cpu_seconds),
    ):
        limit = getattr(resource, name, None)
        if limit is None:
            continue
        try:
            resource.setrlimit(limit, (value, value))
        except (ValueError, OSError):
            continue


def _limit(limits: dict[str, object], name: str) -> float:
    value = limits.get(name, 0)
    return float(value) if value is not None else 0.0


def _probe_pdf(request: dict[str, object]) -> dict[str, object]:
    path = str(request["path"])
    limits = request.get("limits") or {}
    if not isinstance(limits, dict):
        limits = {}

    with open(path, "rb") as handle:
        if handle.read(5) != b"%PDF-":
            return _failure("corrupt", "missing %PDF- header")

    try:
        import pypdf
        from pypdf.errors import PyPdfError
    except ImportError:
        return _failure("corrupt", "pypdf is unavailable")

    max_pages = int(_limit(limits, "max_pdf_pages"))
    max_text = int(_limit(limits, "max_pdf_text_bytes"))
    deadline = time.monotonic() + max(_limit(limits, "timeout_seconds") - 1.0, 0.0)

    try:
        with open(path, "rb") as handle:
            reader = pypdf.PdfReader(handle, strict=False)
            if reader.is_encrypted:
                return _failure("encrypted", "the document is password protected")
            pages = len(reader.pages)
            if pages > max_pages:
                return _failure("too_many_pages", f"pages={pages} limit={max_pages}")
            text_bytes = 0
            for page in reader.pages:
                if time.monotonic() > deadline:
                    return _failure("timeout", "text extraction exceeded the deadline")
                extracted = page.extract_text() or ""
                text_bytes += len(extracted.encode("utf-8"))
                if text_bytes > max_text:
                    return _failure(
                        "text_too_large",
                        f"text_bytes={text_bytes} limit={max_text}",
                    )
    except (PyPdfError, ValueError, OSError, KeyError) as exc:
        return _failure("corrupt", type(exc).__name__)

    return {
        "ok": True,
        "detected_mime": "application/pdf",
        "pages": pages,
        "text_bytes": text_bytes,
    }


def _probe_image(request: dict[str, object]) -> dict[str, object]:
    import warnings

    from PIL import Image, ImageOps, PngImagePlugin, UnidentifiedImageError

    path = str(request["path"])
    declared_mime = str(request.get("mime") or "")
    mode = str(request.get("mode") or "header")
    output = request.get("output")
    limits = request.get("limits") or {}
    if not isinstance(limits, dict):
        limits = {}

    max_pixels = int(_limit(limits, "max_image_pixels"))
    max_edge = int(_limit(limits, "max_image_edge"))
    Image.MAX_IMAGE_PIXELS = max_pixels

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                detected_mime = _FORMAT_TO_MIME.get(image.format or "")
                if detected_mime is None:
                    return _failure(
                        "mime_mismatch",
                        f"format={image.format or 'unknown'} declared={declared_mime}",
                    )
                if detected_mime != declared_mime:
                    return _failure(
                        "mime_mismatch",
                        f"format={detected_mime} declared={declared_mime}",
                    )
                width, height = image.size
                if width * height > max_pixels:
                    return _failure(
                        "too_many_pixels",
                        f"pixels={width * height} limit={max_pixels}",
                    )
                if mode == "header":
                    return {
                        "ok": True,
                        "detected_mime": detected_mime,
                        "width": width,
                        "height": height,
                        "stored_width": width,
                        "stored_height": height,
                    }

                image.load()
                stripped = ImageOps.exif_transpose(image)
                if stripped is None:
                    stripped = image
                stripped.thumbnail((max_edge, max_edge), Image.LANCZOS)
                stored_width, stored_height = stripped.size

                icc_profile = None
                if detected_mime in {"image/jpeg", "image/webp"}:
                    icc_profile = image.info.get("icc_profile")

                save_kwargs: dict[str, object] = {
                    "format": _MIME_TO_FORMAT[detected_mime],
                }
                if detected_mime == "image/jpeg":
                    if stripped.mode not in {"RGB", "L"}:
                        stripped = stripped.convert("RGB")
                    save_kwargs["quality"] = _JPEG_QUALITY
                elif detected_mime == "image/webp":
                    save_kwargs["quality"] = _WEBP_QUALITY
                if icc_profile:
                    save_kwargs["icc_profile"] = icc_profile
                if detected_mime == "image/png":
                    save_kwargs["pnginfo"] = PngImagePlugin.PngInfo()

                if output:
                    stripped.save(str(output), **save_kwargs)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        return _failure("too_many_pixels", f"limit={max_pixels}")
    except (UnidentifiedImageError, OSError) as exc:
        return _failure("decode_failed", type(exc).__name__)

    return {
        "ok": True,
        "detected_mime": detected_mime,
        "width": width,
        "height": height,
        "stored_width": stored_width,
        "stored_height": stored_height,
    }


def _probe_archive(request: dict[str, object]) -> dict[str, object]:
    path = str(request["path"])
    limits = request.get("limits") or {}
    if not isinstance(limits, dict):
        limits = {}

    max_entries = int(_limit(limits, "max_archive_entries"))
    max_uncompressed = int(_limit(limits, "max_archive_uncompressed_bytes"))
    max_ratio = _limit(limits, "max_archive_ratio")

    try:
        archive_size = os.path.getsize(path)
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            entries = len(infos)
            if entries > max_entries:
                return _failure(
                    "too_many_entries",
                    f"entries={entries} limit={max_entries}",
                )

            files = [info for info in infos if not info.is_dir()]
            declared_total = sum(info.file_size for info in files)
            if declared_total > max_uncompressed:
                return _failure(
                    "too_much_uncompressed",
                    f"declared_bytes={declared_total} limit={max_uncompressed}",
                )
            ratio = declared_total / max(archive_size, 1)
            if ratio > max_ratio:
                return _failure(
                    "ratio_too_high",
                    f"ratio={ratio:.1f} limit={max_ratio:.1f}",
                )

            # Read every entry through the streaming inflater.  A member's
            # central-directory size may lie (and zipfile would then truncate
            # the read), so lift the declared ceiling for the duration of the
            # read: the running byte count is what enforces the cap.
            running = 0
            for info in files:
                declared_size = info.file_size
                info.file_size = max(declared_size, 1 << 62)
                try:
                    with archive.open(info) as handle:
                        while True:
                            chunk = handle.read(_STREAM_CHUNK_BYTES)
                            if not chunk:
                                break
                            running += len(chunk)
                            if running > max_uncompressed:
                                return _failure(
                                    "too_much_uncompressed",
                                    f"inflated_bytes={running} limit={max_uncompressed}",
                                )
                finally:
                    info.file_size = declared_size

            compressed_bytes = sum(info.compress_size for info in files)
    except (zipfile.BadZipFile, OSError, EOFError) as exc:
        return _failure("corrupt", type(exc).__name__)

    return {
        "ok": True,
        "detected_mime": "application/zip",
        "entries": entries,
        "uncompressed_bytes": declared_total,
        "compressed_bytes": compressed_bytes,
        "ratio": ratio,
    }


def _unexpected_reason(kind: str) -> str:
    if kind == "image":
        return "decode_failed"
    return "corrupt"


def _run() -> None:
    kind = ""
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict):
            _emit(_failure("corrupt", "request is not an object"))
            return
        kind = str(request.get("kind") or "")
        limits = request.get("limits") or {}
        if isinstance(limits, dict):
            _apply_rlimits(limits)
        if kind == "pdf":
            payload = _probe_pdf(request)
        elif kind == "image":
            payload = _probe_image(request)
        elif kind == "archive":
            payload = _probe_archive(request)
        else:
            payload = _failure("corrupt", f"unknown kind {kind!r}")
        _emit(payload)
    except MemoryError:
        _emit(_failure("decode_failed", "out of memory"))
    except BaseException as exc:
        _emit(_failure(_unexpected_reason(kind), type(exc).__name__))


if __name__ == "__main__":
    _run()
