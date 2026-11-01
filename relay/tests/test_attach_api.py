"""In-process ASGI tests for the attachment upload endpoints.

Every test builds an app from explicit settings with the ``make_app`` fixture;
uploads go through ``TestClient`` (no sockets) and data lives under
``tmp_path``.  Audit assertions read the append-only file written under
``DATA_DIR``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from relay.storage.inbox import InboxStore
from relay.tests import media_fixtures

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PDF_BYTES = media_fixtures.build_pdf()
_WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 24
_TEXT_BYTES = b"hello from the relay\n"
_AUTH = {"Authorization": "Bearer test-token"}
_MB = 1024 * 1024
_60_MB = 60 * _MB
_EXCLUDED_REPO_PARTS = frozenset(
    {".git", ".venv", "build", "__pycache__", ".ruff_cache", ".pytest_cache"}
)


def _png_bytes() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (12, 34, 56)).save(buffer, format="PNG")
    return buffer.getvalue()


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


class _SpyInbox:
    """An inbox whose ingest must not run when the fast pre-check fires."""

    def __init__(self) -> None:
        self.called = False

    async def ingest(self, **kwargs: Any) -> None:
        self.called = True
        raise AssertionError("ingest should not have been called")


def _app(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
    **overrides: Any,
) -> FastAPI:
    settings = {
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
    method: str = "post",
    auth: bool = True,
) -> Any:
    headers = dict(_AUTH) if auth else {}
    return client.request(
        method,
        "/v1/attachments",
        files={"file": (filename, payload, content_type)},
        headers=headers,
    )


def _stored_files(inbox: Path) -> list[Path]:
    if not inbox.exists():
        return []
    return [path for path in inbox.iterdir() if not path.name.startswith(".")]


def test_upload_requires_device_token(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path)
    with TestClient(app) as client:
        response = _upload(
            client,
            filename="photo.png",
            payload=_png_bytes(),
            content_type="image/png",
            auth=False,
        )
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


@pytest.mark.parametrize(
    ("filename", "payload", "content_type", "kind"),
    [
        ("photo.png", _png_bytes(), "image/png", "image"),
        ("report.pdf", _PDF_BYTES, "application/pdf", "document"),
        ("memo.wav", _WAV_BYTES, "audio/wav", "audio"),
        ("notes.txt", _TEXT_BYTES, "text/plain", "text"),
    ],
)
def test_accepts_allowlisted_upload(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
    filename: str,
    payload: bytes,
    content_type: str,
    kind: str,
) -> None:
    app = _app(make_app, tmp_path)
    with TestClient(app) as client:
        response = _upload(
            client,
            filename=filename,
            payload=payload,
            content_type=content_type,
        )
    assert response.status_code == 201, response.text
    body = response.json()
    attachment = body["attachment"]
    assert attachment["kind"] == kind
    assert attachment["mime"] == content_type
    assert len(attachment["sha256"]) == 64
    assert attachment["size_bytes"] > 0
    assert body["spoken"]
    stored = Path(attachment["path"])
    assert stored.is_relative_to(tmp_path / "inbox")
    assert stored.is_file()


def test_put_upload_works(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path)
    with TestClient(app) as client:
        response = _upload(
            client,
            filename="photo.png",
            payload=_png_bytes(),
            content_type="image/png",
            method="put",
        )
    assert response.status_code == 201
    assert response.json()["attachment"]["mime"] == "image/png"


def test_renamed_exe_is_rejected_with_spoken_and_not_stored(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path)
    with TestClient(app) as client:
        response = _upload(
            client,
            filename="setup.exe",
            payload=b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 64,
            content_type="application/octet-stream",
        )
    assert response.status_code == 415
    body = response.json()
    assert body["error"] == "unsupported_type"
    assert body["spoken"]
    assert _stored_files(tmp_path / "inbox") == []


def test_60_mb_upload_is_rejected_with_spoken_and_not_stored(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path)
    payload = _PNG_MAGIC + b"\x00" * _60_MB
    with TestClient(app) as client:
        response = _upload(
            client,
            filename="huge.png",
            payload=payload,
            content_type="image/png",
        )
    assert response.status_code == 413
    body = response.json()
    assert body["error"] == "file_too_large"
    assert body["spoken"]
    assert _stored_files(tmp_path / "inbox") == []


def test_oversized_content_length_fails_before_ingest(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path, max_attachment_mb=1)
    spy = _SpyInbox()
    app.state.inbox = spy
    payload = _PNG_MAGIC + b"\x00" * (3 * _MB)
    with TestClient(app) as client:
        response = _upload(
            client,
            filename="big.png",
            payload=payload,
            content_type="image/png",
        )
    assert response.status_code == 413
    assert response.json()["spoken"]
    assert spy.called is False


def test_streaming_cap_rejects_a_fake_stream(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(make_app, tmp_path)

    @app.post("/v1/_probe/ingest")
    async def probe(request: Request) -> dict[str, bool]:
        store: InboxStore | None = request.app.state.inbox
        assert store is not None
        await store.ingest(
            filename="huge.png",
            declared_content_type="image/png",
            stream=RepeatingStream(_PNG_MAGIC, _60_MB),
        )
        return {"ok": True}

    with TestClient(app) as client:
        response = client.post("/v1/_probe/ingest")
    assert response.status_code == 413
    body = response.json()
    assert body["error"] == "file_too_large"
    assert body["spoken"]
    assert _stored_files(tmp_path / "inbox") == []
    assert list((tmp_path / "inbox").glob(".incoming-*.part")) == []


def test_upload_busy_when_no_slot_frees(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = _app(
        make_app,
        tmp_path,
        max_concurrent_uploads=1,
        reject_retry_after_seconds=1,
    )
    holder = app.state.limits.acquire_upload("holder")
    try:
        with TestClient(app) as client:
            response = _upload(
                client,
                filename="photo.png",
                payload=_png_bytes(),
                content_type="image/png",
            )
    finally:
        holder.release()

    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "upload_busy"
    assert body["spoken"] == "The relay is busy saving another file. Try again in a moment."
    assert body["retry_after"] == 1
    assert response.headers["Retry-After"] == "1"
    assert _stored_files(tmp_path / "inbox") == []


def test_unconfigured_inbox_returns_spoken_503(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    app = make_app(inbox_dir=None, device_tokens_raw="test-token")
    with TestClient(app) as client:
        response = _upload(
            client,
            filename="photo.png",
            payload=_png_bytes(),
            content_type="image/png",
        )
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "inbox_not_configured"
    assert body["spoken"]
    assert app.state.inbox is None


def test_health_stays_ok_without_inbox(
    make_app: Callable[..., FastAPI],
) -> None:
    app = make_app(inbox_dir=None, device_tokens_raw="test-token")
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_audit_records_accepted_upload(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    app = _app(make_app, tmp_path, data_dir=data_dir)
    with TestClient(app) as client:
        response = _upload(
            client,
            filename="../../photos/photo.png",
            payload=_png_bytes(),
            content_type="image/png",
        )
    assert response.status_code == 201

    audit_path = data_dir / "audit.log"
    assert audit_path.is_file()
    entries = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    accepted = [entry for entry in entries if entry.get("event") == "attachment.accepted"]
    assert accepted
    entry = accepted[-1]
    assert entry["filename"] == response.json()["attachment"]["id"]
    assert entry["filename"].startswith("photo-")
    assert len(str(entry["sha256"])) == 64
    assert entry["mime"] == "image/png"
    assert entry["size_bytes"] > 0


def test_upload_creates_nothing_inside_the_repository(
    make_app: Callable[..., FastAPI],
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]

    def snapshot() -> set[str]:
        files: set[str] = set()
        for path in repo_root.rglob("*"):
            relative = path.relative_to(repo_root)
            if any(part in _EXCLUDED_REPO_PARTS for part in relative.parts):
                continue
            if path.is_file():
                files.add(relative.as_posix())
        return files

    app = _app(make_app, tmp_path)
    client = TestClient(app)
    before = snapshot()
    response = _upload(
        client,
        filename="photo.png",
        payload=_png_bytes(),
        content_type="image/png",
    )
    after = snapshot()
    assert response.status_code == 201
    assert before == after
