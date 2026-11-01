"""Offline unit tests for the attachment inbox.

Synthetic fixtures only: images are generated in memory with Pillow, audio and
documents are minimal magic-byte headers, and every path comes from
``tmp_path`` (which pytest places outside the repository).  The suite drives
the async store with ``asyncio.run`` so it needs no pytest plugin.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import stat
import uuid
from pathlib import Path

import pytest
from PIL import Image
from relay.config import Settings
from relay.storage import validation
from relay.storage.inbox import (
    ALLOWED_TYPES,
    INLINE_IMAGE_MAX_BYTES,
    MAX_SNIFF_BYTES,
    AllowedType,
    Attachment,
    AttachmentNotFoundError,
    CorruptMediaError,
    EmptyUploadError,
    FileTooLargeError,
    InboxConfigError,
    InboxStore,
    TypeMismatchError,
    UnsupportedTypeError,
    canonical_extension,
    extension_matches,
    sanitize_filename,
    sniff_mime,
)
from relay.tests import media_fixtures

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff\xe0"
_GIF_MAGIC = b"GIF89a"
_PDF_BYTES = media_fixtures.build_pdf()
_WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 24
_TEXT_BYTES = b"hello from the relay\n"
_MB = 1024 * 1024
_60_MB = 60 * _MB


def _png_bytes(width: int = 4, height: int = 4) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (12, 34, 56)).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_with_exif() -> bytes:
    image = Image.new("RGB", (8, 8), (90, 80, 70))
    exif = Image.Exif()
    exif[0x010F] = "SyntheticCam"
    exif[0x8825] = {1: "N", 2: (1.0, 2.0, 3.0)}
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(_env_file=None, inbox_dir=tmp_path / "inbox", **overrides)


def _store(tmp_path: Path, **overrides: object) -> InboxStore:
    return InboxStore(_settings(tmp_path, **overrides))


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


class RepeatingStream:
    """An async source of ``total`` bytes without allocating them all."""

    def __init__(self, head: bytes, total: int, chunk_size: int = 65536) -> None:
        self._head = head
        self._total = total
        self._sent = 0
        self._chunk_size = chunk_size

    async def read(self, size: int = -1) -> bytes:
        if self._sent >= self._total:
            return b""
        remaining = self._total - self._sent
        wanted = remaining if size is None or size < 0 else min(size, remaining)
        wanted = min(wanted, self._chunk_size)
        if self._sent < len(self._head):
            chunk = self._head[self._sent : self._sent + wanted]
        else:
            chunk = b"\x00" * wanted
        self._sent += len(chunk)
        return chunk


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (_PNG_MAGIC, "image/png"),
        (_JPEG_MAGIC, "image/jpeg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (_GIF_MAGIC, "image/gif"),
        (_PDF_BYTES, "application/pdf"),
        (b"PK\x03\x04\x14\x00\x00\x00", "application/zip"),
        (_WAV_BYTES, "audio/wav"),
        (b"ID3\x04\x00\x00\x00\x00", "audio/mpeg"),
        (b"\xff\xfb\x90\x00", "audio/mpeg"),
        (b"OggS\x00\x02", "audio/ogg"),
        (b"fLaC\x00\x00\x00\x22", "audio/flac"),
        (b"\x00\x00\x00\x18ftypM4A ", "audio/mp4"),
        (b"\x1aE\xdf\xa3\x01\x00\x00\x00", "audio/webm"),
        (_TEXT_BYTES, "text/plain"),
    ],
)
def test_sniff_accepts_allowlisted_magic(head: bytes, expected: str) -> None:
    assert sniff_mime(head) == expected
    assert expected in ALLOWED_TYPES


@pytest.mark.parametrize(
    "head",
    [
        b"",
        b"MZ\x90\x00\x03\x00\x00\x00",
        b"\x00\x01\x02\x03\xff\xfe",
    ],
)
def test_sniff_rejects_non_allowlisted_bytes(head: bytes) -> None:
    assert sniff_mime(head) is None


def test_sniff_does_not_trust_extensions() -> None:
    # The helper only sees bytes; the caller pairs it with extension checks.
    assert sniff_mime(b"MZ" + b"\x00" * 32) is None


@pytest.mark.parametrize(
    ("mime", "filename"),
    [
        ("image/png", "PICTURE.PNG"),
        ("image/jpeg", "photo.JpEg"),
        ("audio/ogg", "voice.OPUS"),
        ("text/plain", "notes.MD"),
        ("audio/mp4", "memo.M4A"),
    ],
)
def test_extension_matches_is_case_insensitive(mime: str, filename: str) -> None:
    assert extension_matches(mime, filename) is True


@pytest.mark.parametrize(
    ("mime", "filename"),
    [
        ("image/png", "photo.pdf"),
        ("image/png", "photo"),
        ("application/pdf", "report.txt"),
        ("text/plain", "notes.png"),
    ],
)
def test_extension_matches_rejects_wrong_family(mime: str, filename: str) -> None:
    assert extension_matches(mime, filename) is False


def test_canonical_extensions_are_allowlisted() -> None:
    for mime, allowed in ALLOWED_TYPES.items():
        assert isinstance(allowed, AllowedType)
        assert canonical_extension(mime) == allowed.extensions[0]
        assert canonical_extension(mime).startswith(".")
    assert canonical_extension("image/jpeg") == ".jpg"
    assert canonical_extension("text/plain") == ".txt"
    assert canonical_extension("application/zip") == ".zip"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd.png", "passwd"),
        ("C:\\Users\\someone\\photo.png", "photo"),
        ("!!weird name!!.txt", "weird-name"),
        ("", "attachment"),
        ("...", "attachment"),
        ("a" * 200 + ".png", "a" * 64),
    ],
)
def test_sanitize_filename_is_basename_and_bounded(raw: str, expected: str) -> None:
    result = sanitize_filename(raw)
    assert result == expected
    assert "/" not in result
    assert "\\" not in result


def test_sanitize_filename_strips_path_components() -> None:
    assert sanitize_filename("/tmp/nested/My File!.PNG") == "My-File"  # noqa: S108


def test_ingest_valid_png_stores_hash_and_mode(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        attachment = await store.ingest(
            filename="../../etc/My Photo!.PNG",
            declared_content_type="image/png",
            stream=FakeStream(_png_bytes()),
        )

        assert attachment.path.parent == store.root
        assert attachment.path.is_file()
        assert stat.S_IMODE(attachment.path.stat().st_mode) == 0o600
        stored = attachment.path.read_bytes()
        assert attachment.sha256 == hashlib.sha256(stored).hexdigest()
        assert attachment.size_bytes == len(stored)
        assert attachment.mime == "image/png"
        assert attachment.kind == "image"
        assert attachment.original_name == "My-Photo.png"
        assert attachment.path.name.startswith("My-Photo-")
        assert attachment.path.name.endswith(".png")
        assert attachment.id == attachment.path.name
        assert attachment.created_at

    asyncio.run(scenario())


def test_ingest_rejects_type_mismatch(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        with pytest.raises(TypeMismatchError):
            await store.ingest(
                filename="picture.pdf",
                declared_content_type="image/png",
                stream=FakeStream(_png_bytes()),
            )
        assert list(store.root.iterdir()) == []

    asyncio.run(scenario())


def test_ingest_rejects_renamed_exe(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        with pytest.raises(UnsupportedTypeError):
            await store.ingest(
                filename="setup.exe",
                declared_content_type="application/octet-stream",
                stream=FakeStream(b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 64),
            )
        assert list(store.root.iterdir()) == []

    asyncio.run(scenario())


def test_ingest_traversal_filename_stays_inside_root(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        attachment = await store.ingest(
            filename="../../etc/passwd.png",
            declared_content_type="image/png",
            stream=FakeStream(_png_bytes()),
        )
        assert attachment.path.parent == store.root
        assert attachment.path.name.startswith("passwd-")
        assert attachment.path.name.endswith(".png")

    asyncio.run(scenario())


def test_ingest_backslash_filename_stays_inside_root(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        attachment = await store.ingest(
            filename="C:\\Users\\someone\\photo.png",
            declared_content_type="image/png",
            stream=FakeStream(_png_bytes()),
        )
        assert attachment.path.parent == store.root
        assert attachment.path.name.startswith("photo-")
        assert attachment.original_name == "photo.png"

    asyncio.run(scenario())


def test_ingest_strips_jpeg_exif_and_gps(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        attachment = await store.ingest(
            filename="camera.jpg",
            declared_content_type="image/jpeg",
            stream=FakeStream(_jpeg_with_exif()),
        )

        with Image.open(attachment.path) as stored:
            exif = stored.getexif()
            assert dict(exif) == {}
            assert 0x8825 not in exif
            assert exif.get_ifd(0x8825) == {}
        assert attachment.sha256 == hashlib.sha256(attachment.path.read_bytes()).hexdigest()

    asyncio.run(scenario())


def test_ingest_60_mb_stream_hits_cap_and_leaves_no_temp(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        with pytest.raises(FileTooLargeError):
            await store.ingest(
                filename="huge.png",
                declared_content_type="image/png",
                stream=RepeatingStream(_PNG_MAGIC, _60_MB),
            )
        assert list(store.root.glob(".incoming-*.part")) == []
        assert list(store.root.iterdir()) == []

    asyncio.run(scenario())


def test_cap_is_read_from_settings(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path, max_attachment_mb=1)
        with pytest.raises(FileTooLargeError):
            await store.ingest(
                filename="over.png",
                declared_content_type="image/png",
                stream=RepeatingStream(_PNG_MAGIC, 2 * _MB),
            )
        assert list(store.root.glob(".incoming-*.part")) == []

    asyncio.run(scenario())


def test_ingest_empty_upload_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        with pytest.raises(EmptyUploadError):
            await store.ingest(
                filename="empty.txt",
                declared_content_type="text/plain",
                stream=FakeStream(b""),
            )
        assert list(store.root.iterdir()) == []

    asyncio.run(scenario())


def test_ingest_truncated_png_is_corrupt(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        with pytest.raises(CorruptMediaError):
            await store.ingest(
                filename="truncated.png",
                declared_content_type="image/png",
                stream=FakeStream(_PNG_MAGIC + b"garbage-not-a-png"),
            )
        assert list(store.root.glob(".incoming-*.part")) == []
        assert list(store.root.iterdir()) == []

    asyncio.run(scenario())


def test_inbox_inside_repo_is_refused_and_not_created() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    inside = repo_root / f".inbox-config-test-{uuid.uuid4().hex}"
    assert not inside.exists()
    with pytest.raises(InboxConfigError):
        InboxStore(Settings(_env_file=None, inbox_dir=inside))
    assert not inside.exists()


def test_missing_inbox_root_is_created_with_0700(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert not store.root.exists()
    store.ensure_ready()
    assert store.root.is_dir()
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700


def test_resolve_returns_confined_path_and_rejects_escapes(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        attachment = await store.ingest(
            filename="doc.pdf",
            declared_content_type="application/pdf",
            stream=FakeStream(_PDF_BYTES),
        )
        assert store.resolve(attachment.id) == attachment.path

        with pytest.raises(AttachmentNotFoundError):
            store.resolve("does-not-exist.pdf")
        with pytest.raises(AttachmentNotFoundError):
            store.resolve("../escape.pdf")
        with pytest.raises(AttachmentNotFoundError):
            store.resolve("/etc/passwd.pdf")

        outside = tmp_path / "outside.txt"
        outside.write_text("not in the inbox\n", encoding="utf-8")
        link = store.root / "link.txt"
        link.symlink_to(outside)
        with pytest.raises(AttachmentNotFoundError):
            store.resolve("link.txt")

    asyncio.run(scenario())


def test_delegation_parts_inline_image_round_trips_stored_bytes(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        attachment = await store.ingest(
            filename="photo.png",
            declared_content_type="image/png",
            stream=FakeStream(_png_bytes()),
        )
        parts = store.delegation_parts([attachment])

        assert len(parts) == 2
        text_part = parts[0]
        assert text_part["type"] == "text"
        text = str(text_part["text"])
        assert "Attached file 1/1:" in text
        assert f"path={attachment.path}" in text
        assert f"sha256={attachment.sha256}" in text
        assert f"mime={attachment.mime}" in text
        assert f"bytes={attachment.size_bytes}" in text
        assert f"name={attachment.original_name}" in text

        image_part = parts[1]
        assert image_part["type"] == "image_url"
        image_url = image_part["image_url"]
        assert isinstance(image_url, dict)
        url = str(image_url["url"])
        assert url.startswith("data:image/png;base64,")
        encoded = url.split(",", 1)[1]
        assert base64.b64decode(encoded) == attachment.path.read_bytes()

    asyncio.run(scenario())


def test_delegation_parts_pdf_has_no_image_part(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        attachment = await store.ingest(
            filename="report.pdf",
            declared_content_type="application/pdf",
            stream=FakeStream(_PDF_BYTES),
        )
        parts = store.delegation_parts([attachment])
        assert len(parts) == 1
        assert parts[0]["type"] == "text"
        assert "report.pdf" in str(parts[0]["text"])

    asyncio.run(scenario())


def test_delegation_parts_respect_inline_images_flag(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        attachment = await store.ingest(
            filename="photo.png",
            declared_content_type="image/png",
            stream=FakeStream(_png_bytes()),
        )
        parts = store.delegation_parts([attachment], inline_images=False)
        assert len(parts) == 1
        assert parts[0]["type"] == "text"

    asyncio.run(scenario())


def test_delegation_parts_empty_input_is_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.delegation_parts([]) == []


def test_delegation_parts_missing_file_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    missing = Attachment(
        id="gone.png",
        path=store.root / "gone.png",
        mime="image/png",
        kind="image",
        sha256="0" * 64,
        size_bytes=10,
        original_name="gone.png",
        created_at="2026-01-01T00:00:00+00:00",
    )
    with pytest.raises(AttachmentNotFoundError):
        store.delegation_parts([missing])


def test_constants_match_specification() -> None:
    assert MAX_SNIFF_BYTES == 4096
    assert INLINE_IMAGE_MAX_BYTES == 8 * 1024 * 1024
    assert validation.DEFAULT_LIMITS.max_image_pixels == 40_000_000
    assert validation.DEFAULT_LIMITS.max_pdf_pages == 200
    assert validation.DEFAULT_LIMITS.max_archive_entries == 512
    assert validation.DEFAULT_LIMITS.parse_timeout_seconds == 10.0


def test_ingest_gif_is_copied_through_unchanged(tmp_path: Path) -> None:
    async def scenario() -> None:
        buffer = io.BytesIO()
        Image.new("P", (4, 4), color=3).save(buffer, format="GIF")
        original = buffer.getvalue()

        store = _store(tmp_path)
        attachment = await store.ingest(
            filename="animation.gif",
            declared_content_type="image/gif",
            stream=FakeStream(original),
        )
        assert attachment.path.read_bytes() == original

    asyncio.run(scenario())
