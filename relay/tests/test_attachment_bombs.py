"""Attachment bomb, malformed-file, and parser-isolation tests.

All fixtures are generated locally under ``tmp_path``; nothing is committed and
nothing opens a network socket.  Structural parsing runs in the isolated probe
child process, so the relay process itself never decodes media.
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import io
import os
import socket
import struct
import time
import tracemalloc
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from relay.config import Settings
from relay.storage import validation
from relay.storage.errors import (
    ArchiveCorruptError,
    ArchiveTooComplexError,
    CorruptDocumentError,
    DocumentTooComplexError,
    EmptyUploadError,
    EncryptedDocumentError,
    ImageTooLargeError,
    IntegrityError,
    ParseTimeoutError,
    TypeMismatchError,
)
from relay.storage.inbox import InboxStore
from relay.storage.validation import ValidationLimits
from relay.tests import media_fixtures

_MB = 1024 * 1024
_AUTH = {"Authorization": "Bearer test-token"}
_ALLOWED_ERROR_KEYS = {
    "error",
    "detail",
    "spoken",
    "retry_after",
    "correlation_id",
}
_PROBE_NAME = "_probe.py"


class FakeStream:
    """An async byte source that yields bounded chunks."""

    def __init__(self, data: bytes, chunk_size: int = 8192) -> None:
        self._data = data
        self._offset = 0
        self._chunk_size = chunk_size

    async def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._data):
            return b""
        remaining = len(self._data) - self._offset
        wanted = remaining if size is None or size < 0 else min(size, remaining)
        wanted = min(wanted, self._chunk_size)
        end = self._offset + wanted
        chunk = self._data[self._offset : end]
        self._offset = end
        return chunk


def _store(tmp_path: Path, limits: ValidationLimits | None = None) -> InboxStore:
    settings = Settings(_env_file=None, inbox_dir=tmp_path / "inbox")
    return InboxStore(settings, limits=limits)


async def _ingest(
    store: InboxStore,
    *,
    filename: str,
    payload: bytes,
    content_type: str,
) -> Any:
    return await store.ingest(
        filename=filename,
        declared_content_type=content_type,
        stream=FakeStream(payload),
    )


def _vm_rss_bytes() -> int | None:
    try:
        status = Path("/proc/self/status").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024
    return None


def _live_probe_children() -> list[str]:
    remaining: list[str] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return remaining
    parent_pid = os.getpid()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
            stat = (entry / "stat").read_text(encoding="utf-8")
        except OSError:
            continue
        if _PROBE_NAME.encode() not in cmdline:
            continue
        try:
            fields = stat.rsplit(")", 1)[1].split()
            ppid = int(fields[1])
        except (IndexError, ValueError):
            continue
        if ppid == parent_pid:
            remaining.append(entry.name)
    return remaining


def _zip_with_payload(size: int) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("payload.bin", b"\x00" * size)
    return buffer.getvalue()


def _lie_about_uncompressed_size(data: bytes, declared: int = 10) -> bytes:
    """Patch the central-directory file_size so it understates the member."""
    patched = bytearray(data)
    signature = b"PK\x01\x02"
    start = 0
    while True:
        offset = patched.find(signature, start)
        if offset < 0:
            break
        struct.pack_into("<I", patched, offset + 24, declared)
        start = offset + 4
    return bytes(patched)


def _app(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
    **overrides: Any,
) -> FastAPI:
    settings: dict[str, Any] = {
        "inbox_dir": tmp_path / "inbox",
        "device_tokens_raw": "test-token",
    }
    settings.update(overrides)
    return make_app(**settings)


def _upload(
    client: TestClient,
    *,
    filename: str,
    payload: bytes,
    content_type: str,
) -> Any:
    return client.post(
        "/v1/attachments",
        files={"file": (filename, payload, content_type)},
        headers=_AUTH,
    )


def _assert_rejection_body(response: Any) -> dict[str, Any]:
    body = response.json()
    assert body["spoken"]
    assert set(body) <= _ALLOWED_ERROR_KEYS
    assert "Traceback" not in response.text
    return body


def test_zip_bomb_rejected_fast_without_extraction(tmp_path: Path) -> None:
    payload = media_fixtures.zip_bomb()

    async def scenario() -> None:
        store = _store(tmp_path)
        started = time.monotonic()
        with pytest.raises(ArchiveTooComplexError) as caught:
            await _ingest(
                store,
                filename="bomb.zip",
                payload=payload,
                content_type="application/zip",
            )
        elapsed = time.monotonic() - started
        assert elapsed < 2.0
        assert caught.value.spoken
        assert caught.value.code == "archive_too_complex"
        assert list(store.root.iterdir()) == []

    asyncio.run(scenario())


def test_100mp_png_rejected_without_relay_decode(tmp_path: Path) -> None:
    payload = media_fixtures.big_png()
    gc.collect()
    baseline = _vm_rss_bytes()

    async def scenario() -> None:
        store = _store(tmp_path)
        started = time.monotonic()
        with pytest.raises(ImageTooLargeError) as caught:
            await _ingest(
                store,
                filename="huge.png",
                payload=payload,
                content_type="image/png",
            )
        elapsed = time.monotonic() - started
        assert elapsed < 2.0
        assert caught.value.spoken
        assert caught.value.code == "image_too_large"
        assert list(store.root.iterdir()) == []

    asyncio.run(scenario())
    gc.collect()
    if baseline is not None:
        after = _vm_rss_bytes()
        assert after is not None
        assert after - baseline < 32 * _MB


def test_truncated_pdf_is_corrupt(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        with pytest.raises(CorruptDocumentError) as caught:
            await _ingest(
                store,
                filename="broken.pdf",
                payload=media_fixtures.truncated_pdf(),
                content_type="application/pdf",
            )
        assert caught.value.spoken
        assert list(store.root.iterdir()) == []

    asyncio.run(scenario())


def test_text_renamed_pdf_is_type_mismatch(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        with pytest.raises(TypeMismatchError):
            await _ingest(
                store,
                filename="notes.pdf",
                payload=b"hello from a text file\n",
                content_type="application/pdf",
            )
        assert list(store.root.iterdir()) == []

    asyncio.run(scenario())


def test_zip_renamed_pdf_is_type_mismatch(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        with pytest.raises(TypeMismatchError):
            await _ingest(
                store,
                filename="archive.pdf",
                payload=media_fixtures.valid_zip(),
                content_type="application/pdf",
            )
        assert list(store.root.iterdir()) == []

    asyncio.run(scenario())


def test_zero_byte_pdf_is_empty(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        with pytest.raises(EmptyUploadError):
            await _ingest(
                store,
                filename="empty.pdf",
                payload=b"",
                content_type="application/pdf",
            )
        assert list(store.root.iterdir()) == []

    asyncio.run(scenario())


def test_encrypted_pdf_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        with pytest.raises(EncryptedDocumentError) as caught:
            await _ingest(
                store,
                filename="locked.pdf",
                payload=media_fixtures.encrypted_pdf(),
                content_type="application/pdf",
            )
        assert caught.value.spoken
        assert caught.value.code == "document_encrypted"

    asyncio.run(scenario())


def test_too_many_pdf_pages_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        with pytest.raises(DocumentTooComplexError) as caught:
            await _ingest(
                store,
                filename="long.pdf",
                payload=media_fixtures.build_pdf(pages=300),
                content_type="application/pdf",
            )
        assert caught.value.spoken
        assert caught.value.code == "document_too_complex"

    asyncio.run(scenario())


def test_pdf_text_cap_rejected(tmp_path: Path) -> None:
    limits = ValidationLimits(max_pdf_text_bytes=10)

    async def scenario() -> None:
        store = _store(tmp_path, limits=limits)
        with pytest.raises(DocumentTooComplexError) as caught:
            await _ingest(
                store,
                filename="wordy.pdf",
                payload=media_fixtures.build_pdf(
                    text="Hello world, this line is longer than ten bytes.",
                ),
                content_type="application/pdf",
            )
        assert caught.value.spoken

    asyncio.run(scenario())


def test_valid_pdf_is_accepted_and_recorded(tmp_path: Path) -> None:
    payload = media_fixtures.build_pdf(pages=20)

    async def scenario() -> None:
        store = _store(tmp_path)
        attachment = await _ingest(
            store,
            filename="report.pdf",
            payload=payload,
            content_type="application/pdf",
        )
        assert attachment.detected_mime == "application/pdf"
        assert attachment.facts["pages"] == 20
        assert attachment.uploaded_bytes == len(payload)
        stored = attachment.path.read_bytes()
        assert attachment.sha256 == hashlib.sha256(stored).hexdigest()
        assert attachment.to_public_dict()["content_sha256"] == attachment.sha256
        parts = store.delegation_parts([attachment])
        assert len(parts) == 1
        text = str(parts[0]["text"])
        assert f"path={attachment.path}" in text
        assert f"sha256={attachment.sha256}" in text
        assert "pages=20" in text

    asyncio.run(scenario())


def test_medium_png_is_stored_downscaled(tmp_path: Path) -> None:
    payload = media_fixtures.medium_png(4000, 3000)

    async def scenario() -> None:
        store = _store(tmp_path)
        attachment = await _ingest(
            store,
            filename="wide.png",
            payload=payload,
            content_type="image/png",
        )
        assert attachment.detected_mime == "image/png"
        assert attachment.facts["width"] == 4000
        assert attachment.facts["height"] == 3000
        stored_width = attachment.facts["stored_width"]
        stored_height = attachment.facts["stored_height"]
        assert isinstance(stored_width, int)
        assert isinstance(stored_height, int)
        assert max(stored_width, stored_height) <= 1024
        assert abs(stored_width / stored_height - 4 / 3) < 0.01
        with Image.open(attachment.path) as stored:
            assert stored.size == (stored_width, stored_height)
        assert attachment.path.read_bytes() != payload

    asyncio.run(scenario())


def test_large_gif_rejected_in_header_mode(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    Image.new("1", (8000, 6000)).save(buffer, format="GIF")
    payload = buffer.getvalue()

    async def scenario() -> None:
        store = _store(tmp_path)
        started = time.monotonic()
        with pytest.raises(ImageTooLargeError) as caught:
            await _ingest(
                store,
                filename="huge.gif",
                payload=payload,
                content_type="image/gif",
            )
        assert time.monotonic() - started < 2.0
        assert caught.value.spoken
        assert list(store.root.iterdir()) == []

    asyncio.run(scenario())


def test_archive_with_too_many_entries_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        with pytest.raises(ArchiveTooComplexError) as caught:
            await _ingest(
                store,
                filename="many.zip",
                payload=media_fixtures.many_entries_zip(),
                content_type="application/zip",
            )
        assert caught.value.spoken

    asyncio.run(scenario())


def test_archive_declared_bytes_over_cap_rejected(tmp_path: Path) -> None:
    limits = ValidationLimits(max_archive_uncompressed_bytes=1024)

    async def scenario() -> None:
        store = _store(tmp_path, limits=limits)
        with pytest.raises(ArchiveTooComplexError) as caught:
            await _ingest(
                store,
                filename="big.zip",
                payload=_zip_with_payload(2048),
                content_type="application/zip",
            )
        assert caught.value.spoken

    asyncio.run(scenario())


def test_archive_ratio_bomb_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        with pytest.raises(ArchiveTooComplexError) as caught:
            await _ingest(
                store,
                filename="ratio.zip",
                payload=_zip_with_payload(_MB),
                content_type="application/zip",
            )
        assert caught.value.spoken
        assert caught.value.code == "archive_too_complex"

    asyncio.run(scenario())


def test_password_protected_zip_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        with pytest.raises(ArchiveCorruptError) as caught:
            await _ingest(
                store,
                filename="locked.zip",
                payload=media_fixtures.encrypted_entry_zip(),
                content_type="application/zip",
            )
        assert caught.value.spoken
        assert caught.value.code == "archive_corrupt"
        assert list(store.root.iterdir()) == []

    asyncio.run(scenario())


def test_archive_with_lying_header_is_caught_by_streaming(tmp_path: Path) -> None:
    payload = _lie_about_uncompressed_size(_zip_with_payload(2 * _MB))
    limits = ValidationLimits(max_archive_uncompressed_bytes=_MB)

    async def scenario() -> None:
        store = _store(tmp_path, limits=limits)
        with pytest.raises(ArchiveTooComplexError) as caught:
            await _ingest(
                store,
                filename="liar.zip",
                payload=payload,
                content_type="application/zip",
            )
        assert caught.value.spoken

    asyncio.run(scenario())


def test_valid_zip_is_accepted(tmp_path: Path) -> None:
    payload = media_fixtures.valid_zip()

    async def scenario() -> None:
        store = _store(tmp_path)
        attachment = await _ingest(
            store,
            filename="bundle.zip",
            payload=payload,
            content_type="application/zip",
        )
        assert attachment.kind == "archive"
        assert attachment.detected_mime == "application/zip"
        assert attachment.facts["entries"] == 2
        assert attachment.path.is_file()

    asyncio.run(scenario())


def test_parse_timeout_is_spoken_and_leaves_no_temp(tmp_path: Path) -> None:
    limits = ValidationLimits(parse_timeout_seconds=0.01)

    async def scenario() -> None:
        store = _store(tmp_path, limits=limits)
        with pytest.raises(ParseTimeoutError) as caught:
            await _ingest(
                store,
                filename="slow.png",
                payload=media_fixtures.medium_png(64, 64),
                content_type="image/png",
            )
        assert caught.value.spoken
        assert caught.value.code == "parse_timeout"
        assert list(store.root.iterdir()) == []

    asyncio.run(scenario())
    assert _live_probe_children() == []


def test_delegation_detects_tampered_bytes(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        attachment = await _ingest(
            store,
            filename="photo.png",
            payload=media_fixtures.png_with_exif(),
            content_type="image/png",
        )
        tampered = bytearray(attachment.path.read_bytes())
        tampered[-1] ^= 0xFF
        attachment.path.write_bytes(bytes(tampered))

        with pytest.raises(IntegrityError) as caught:
            store.delegation_parts([attachment])
        assert caught.value.spoken
        assert caught.value.code == "attachment_changed"

    asyncio.run(scenario())


def test_repeated_uploads_keep_relay_memory_flat(tmp_path: Path) -> None:
    payload = media_fixtures.png_with_exif()

    async def scenario() -> None:
        store = _store(tmp_path)
        tracemalloc.start()
        baseline = tracemalloc.get_traced_memory()[0]
        try:
            for index in range(50):
                attachment = await _ingest(
                    store,
                    filename=f"photo-{index}.png",
                    payload=payload,
                    content_type="image/png",
                )
                assert attachment.path.is_file()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak - baseline < 8 * _MB

    asyncio.run(scenario())


def test_http_zip_bomb_rejected_with_spoken_error(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path)
    with TestClient(app) as client:
        response = _upload(
            client,
            filename="bomb.zip",
            payload=media_fixtures.zip_bomb(),
            content_type="application/zip",
        )
    assert response.status_code == 413
    body = _assert_rejection_body(response)
    assert body["error"] == "archive_too_complex"


def test_http_100mp_png_rejected_with_spoken_error(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path)
    with TestClient(app) as client:
        response = _upload(
            client,
            filename="huge.png",
            payload=media_fixtures.big_png(),
            content_type="image/png",
        )
    assert response.status_code == 413
    body = _assert_rejection_body(response)
    assert body["error"] == "image_too_large"


def test_http_truncated_pdf_rejected_with_spoken_error(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path)
    with TestClient(app) as client:
        response = _upload(
            client,
            filename="broken.pdf",
            payload=media_fixtures.truncated_pdf(),
            content_type="application/pdf",
        )
    assert response.status_code == 422
    body = _assert_rejection_body(response)
    assert body["error"] == "corrupt_document"


def test_http_accepts_a_zip_upload(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path)
    with TestClient(app) as client:
        response = _upload(
            client,
            filename="bundle.zip",
            payload=media_fixtures.valid_zip(),
            content_type="application/zip",
        )
    assert response.status_code == 201
    body = response.json()
    assert body["attachment"]["kind"] == "archive"
    assert body["spoken"] == "Archive saved."
    assert body["attachment"]["facts"]["entries"] == 2


def test_validation_makes_no_socket_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    original_connect = socket.socket.connect

    def guarded(self: socket.socket, address: object) -> None:
        calls.append(address)
        return original_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)

    async def scenario() -> None:
        store = _store(tmp_path)
        attachment = await _ingest(
            store,
            filename="report.pdf",
            payload=media_fixtures.build_pdf(),
            content_type="application/pdf",
        )
        assert attachment.path.is_file()

    asyncio.run(scenario())
    assert calls == []


def test_default_limits_match_the_documented_caps() -> None:
    limits = validation.DEFAULT_LIMITS
    assert limits.max_archive_entries == 512
    assert limits.max_archive_uncompressed_bytes == 256 * _MB
    assert limits.max_archive_ratio == 200.0
    assert limits.max_pdf_pages == 200
    assert limits.max_pdf_text_bytes == 2 * _MB
    assert limits.max_image_pixels == 40_000_000
    assert limits.max_image_edge == 1024
    assert limits.parse_timeout_seconds == 10.0
    assert limits.probe_memory_bytes == 1024 * _MB
