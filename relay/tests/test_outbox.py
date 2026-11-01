"""Unit tests for the confined artifact outbox store.

Everything is synthetic and lives under ``tmp_path``; no test writes inside the
repository.  The tests exercise id validation, symlink rejection, expiry,
digest caching, listing filters, MIME selection, and descriptor hygiene.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
import relay.storage.outbox as outbox_module
from relay.config import Settings
from relay.storage.outbox import (
    ArtifactExpiredError,
    ArtifactNotFoundError,
    OutboxConfigError,
    OutboxStore,
    mime_for,
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_SECONDS_PER_DAY = 86400
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PUBLIC_KEYS = {
    "id",
    "name",
    "mime",
    "size_bytes",
    "sha256",
    "created_at",
    "expires_at",
}


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "outbox_dir": tmp_path / "outbox",
        "outbox_retention_days": 0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _store(
    tmp_path: Path,
    *,
    clock: Any = None,
    digest_cache_size: int = 256,
    **overrides: Any,
) -> OutboxStore:
    store = OutboxStore(
        _settings(tmp_path, **overrides),
        clock=clock,
        digest_cache_size=digest_cache_size,
    )
    store.root.mkdir(parents=True, exist_ok=True)
    return store


def _write(
    root: Path,
    relative: str,
    payload: bytes,
    *,
    mtime: float | None = None,
) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "/absolute.txt",
        "back\\slash.txt",
        "nul\x00byte.txt",
        "control\x1fchar.txt",
        "double//slash.txt",
        "trailing/",
        ".",
        "..",
        "a/./b.txt",
        "a/../b.txt",
        ".hidden.txt",
        "nested/.hidden.txt",
        "a" * 256,
        "/".join(["a" * 100] * 11),
        123,
    ],
)
def test_invalid_artifact_ids_are_not_found(tmp_path: Path, bad_id: object) -> None:
    store = _store(tmp_path)
    with pytest.raises(ArtifactNotFoundError):
        store.open_artifact(bad_id)  # type: ignore[arg-type]


def test_symlinks_are_rejected_even_when_inside_the_root(tmp_path: Path) -> None:
    store = _store(tmp_path)
    real = _write(store.root, "real.txt", b"inside")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    (store.root / "link-out.txt").symlink_to(outside)
    (store.root / "link-in.txt").symlink_to(real)

    with pytest.raises(ArtifactNotFoundError):
        store.open_artifact("link-out.txt")
    with pytest.raises(ArtifactNotFoundError):
        store.open_artifact("link-in.txt")


def test_directory_and_missing_ids_are_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    (store.root / "folder").mkdir()

    with pytest.raises(ArtifactNotFoundError):
        store.open_artifact("folder")
    with pytest.raises(ArtifactNotFoundError):
        store.open_artifact("missing.txt")


def test_expired_artifact_is_rejected(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    store = _store(tmp_path, outbox_retention_days=1, clock=lambda: now)
    _write(store.root, "old.txt", b"old", mtime=now - 2 * _SECONDS_PER_DAY)

    with pytest.raises(ArtifactExpiredError):
        store.open_artifact("old.txt")


def test_recent_artifact_is_served(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    store = _store(tmp_path, outbox_retention_days=1, clock=lambda: now)
    _write(store.root, "recent.txt", b"recent", mtime=now - 23 * 3600)

    handle = store.open_artifact("recent.txt")
    try:
        assert handle.read_chunk() == b"recent"
        assert handle.artifact.expires_at is not None
    finally:
        handle.close()


def test_retention_zero_never_expires(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    store = _store(tmp_path, outbox_retention_days=0, clock=lambda: now)
    _write(store.root, "ancient.txt", b"ancient", mtime=now - 10 * _SECONDS_PER_DAY)

    handle = store.open_artifact("ancient.txt")
    try:
        assert handle.artifact.expires_at is None
        assert handle.read_chunk() == b"ancient"
    finally:
        handle.close()


def test_digest_cache_hits_and_rehashes_a_rewritten_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    real_hash = outbox_module._hash_fd

    def counting_hash(descriptor: int) -> str:
        calls.append(descriptor)
        return real_hash(descriptor)

    monkeypatch.setattr(outbox_module, "_hash_fd", counting_hash)
    store = _store(tmp_path)
    path = _write(store.root, "cached.txt", b"first", mtime=1000)

    first = store.open_artifact("cached.txt")
    first.close()
    second = store.open_artifact("cached.txt")
    second.close()
    assert len(calls) == 1

    _write(store.root, "cached.txt", b"second-and-longer", mtime=2000)
    third = store.open_artifact("cached.txt")
    try:
        assert third.artifact.sha256 == hashlib.sha256(b"second-and-longer").hexdigest()
    finally:
        third.close()
    assert len(calls) == 2
    assert path.stat().st_size == len(b"second-and-longer")


def test_list_artifacts_filters_sorts_clamps_and_hides_paths(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    store = _store(tmp_path, outbox_retention_days=1, clock=lambda: now)
    _write(store.root, "newest.txt", b"newest", mtime=now - 10)
    _write(store.root, "older.txt", b"older", mtime=now - 100)
    _write(store.root, "sub/middle.txt", b"middle", mtime=now - 50)
    _write(store.root, ".hidden.txt", b"hidden", mtime=now)
    _write(store.root, "expired.txt", b"expired", mtime=now - 2 * _SECONDS_PER_DAY)
    (store.root / ".locks").mkdir()
    (store.root / ".locks" / "newest.txt.lock").write_text("", encoding="utf-8")
    (store.root / "folder").mkdir()
    (store.root / "link.txt").symlink_to(store.root / "newest.txt")

    artifacts = store.list_artifacts(limit=50)
    assert [artifact.id for artifact in artifacts] == [
        "newest.txt",
        "sub/middle.txt",
        "older.txt",
    ]

    assert [artifact.id for artifact in store.list_artifacts(limit=1)] == ["newest.txt"]
    assert [artifact.id for artifact in store.list_artifacts(limit=0)] == ["newest.txt"]
    assert len(store.list_artifacts(limit=999)) == 3

    public = [artifact.to_public_dict() for artifact in artifacts]
    assert set(public[0]) == _PUBLIC_KEYS
    serialized = json.dumps(public)
    assert str(tmp_path) not in serialized
    assert "path" not in public[0]
    assert public[0]["expires_at"] is not None


@pytest.mark.parametrize(
    ("name", "head", "expected"),
    [
        ("picture.png", b"", "image/png"),
        ("photo.jpg", b"", "image/jpeg"),
        ("photo.jpeg", b"", "image/jpeg"),
        ("image.webp", b"", "image/webp"),
        ("anim.gif", b"", "image/gif"),
        ("report.pdf", b"", "application/pdf"),
        ("notes.txt", b"", "text/plain"),
        ("notes.md", b"", "text/markdown"),
        ("table.csv", b"", "text/csv"),
        ("data.json", b"{}", "application/json"),
        ("page.html", b"", "text/html"),
        ("drawing.svg", b"", "image/svg+xml"),
        ("bundle.zip", b"", "application/zip"),
        ("clip.wav", b"", "audio/wav"),
        ("clip.mp3", b"", "audio/mpeg"),
        ("clip.ogg", b"", "audio/ogg"),
        ("clip.opus", b"", "audio/ogg"),
        ("clip.flac", b"", "audio/flac"),
        ("clip.m4a", b"", "audio/mp4"),
        ("movie.mp4", b"", "video/mp4"),
        ("movie.webm", b"", "video/webm"),
        ("mystery.bin", _PNG_MAGIC + b"\x00" * 16, "image/png"),
        ("mystery.bin", b"\x00\x01\x02\x03", "application/octet-stream"),
    ],
)
def test_mime_for_extension_and_magic(name: str, head: bytes, expected: str) -> None:
    assert mime_for(name, head) == expected


def test_outbox_unset_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(OutboxConfigError):
        OutboxStore(_settings(tmp_path, outbox_dir=None))


def test_outbox_inside_repository_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(OutboxConfigError):
        OutboxStore(_settings(tmp_path, outbox_dir=_REPO_ROOT))


def test_missing_root_is_available_false_and_empty(tmp_path: Path) -> None:
    store = OutboxStore(_settings(tmp_path))

    assert store.available is False
    assert store.list_artifacts() == []
    with pytest.raises(ArtifactNotFoundError):
        store.open_artifact("anything.txt")

    store.root.mkdir()
    assert store.available is True


def test_handle_close_is_idempotent_and_leaks_no_descriptors(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(store.root, "tiny.txt", b"abcdef")

    def descriptor_count() -> int:
        try:
            return len(os.listdir("/proc/self/fd"))
        except OSError:
            return -1

    before = descriptor_count()
    for _iteration in range(25):
        handle = store.open_artifact("tiny.txt")
        assert handle.read_chunk(3) == b"abc"
        handle.close()
        handle.close()
        assert handle.read_chunk(3) == b""
    after = descriptor_count()
    if before >= 0:
        assert after == before


def test_one_mib_artifact_streams_byte_identically(tmp_path: Path) -> None:
    payload = bytes(range(256)) * 4096
    assert len(payload) == 1024 * 1024
    store = _store(tmp_path)
    _write(store.root, "large.bin", payload)

    handle = store.open_artifact("large.bin")
    chunks: list[bytes] = []
    try:
        assert handle.artifact.sha256 == hashlib.sha256(payload).hexdigest()
        while True:
            chunk = handle.read_chunk(64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        handle.close()

    assert b"".join(chunks) == payload


def test_expiry_boundary_uses_configured_clock(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    store = _store(tmp_path, outbox_retention_days=1, clock=lambda: now)
    _write(store.root, "edge.txt", b"edge", mtime=now - _SECONDS_PER_DAY)

    with pytest.raises(ArtifactExpiredError):
        store.open_artifact("edge.txt")


def test_default_clock_is_used_when_none_is_supplied(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(store.root, "now.txt", b"now", mtime=time.time())
    handle = store.open_artifact("now.txt")
    handle.close()


def test_fifo_is_not_found_and_does_not_block(tmp_path: Path) -> None:
    store = _store(tmp_path)
    os.mkfifo(store.root / "pipe")

    with pytest.raises(ArtifactNotFoundError):
        store.open_artifact("pipe")
    assert store.list_artifacts() == []


def test_expired_artifact_is_not_hashed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real_hash = outbox_module._hash_fd

    def counting_hash(descriptor: int) -> str:
        calls.append(descriptor)
        return real_hash(descriptor)

    monkeypatch.setattr(outbox_module, "_hash_fd", counting_hash)
    now = 1_700_000_000.0
    store = _store(tmp_path, outbox_retention_days=1, clock=lambda: now)
    _write(store.root, "old.txt", b"old", mtime=now - 2 * _SECONDS_PER_DAY)

    with pytest.raises(ArtifactExpiredError):
        store.open_artifact("old.txt")
    assert calls == []
