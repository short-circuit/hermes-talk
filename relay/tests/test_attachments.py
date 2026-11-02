"""End-to-end attachment tests: upload -> delegation -> artifact download.

The suite runs the real relay app on a loopback socket and drives it over real
HTTP, so the multipart parser, the device-token dependency, the inbox ingest
path, the artifact listing and the streaming download are all exercised as the
phone would exercise them.  Delegation goes through the real
:class:`relay.hermes.client.HermesClient` against ``HermesStub``, which records
what was submitted; the agent's side effects (the edited picture and the
document summary) are written into the outbox by the test, because the relay
itself never writes there.

Everything is local: the only sockets are ``127.0.0.1``, there is no cloud STT,
TTS or model call, and the fixtures are synthetic files committed under
``fixtures/attachments`` (about 22 KiB in total).

The image path proves the relay inlines the stored bytes as a data URL and that
the machine line the adapter submits carries the absolute path plus the SHA-256
the agent must read the file from; the test edits the *inline* bytes, which is
what the model would see.  ``HermesClient.submit_run`` accepts a string, so the
content parts are flattened into the instruction exactly as the voice layer
must do until the adapter accepts a parts array.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import random
import socket
import threading
import time
import wave
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from PIL import Image
from pypdf import PdfReader
from relay.app import create_app
from relay.brain.delegation import (
    DelegationService,
    DelegationState,
    SpokenUpdate,
    delegation_failure,
    delegation_input,
)
from relay.brain.prompts import ENVELOPE_PREFIX
from relay.config import Settings
from relay.hermes.client import HermesClient
from relay.storage.errors import (
    CorruptDocumentError,
    FileTooLargeError,
    ImageTooLargeError,
)
from relay.storage.inbox import Attachment, InboxStore, sniff_mime
from relay.storage.outbox import ArtifactNotFoundError
from relay.tests._hermes_stub import HermesStub

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "attachments"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXCLUDED_REPO_PARTS = frozenset(
    {".git", ".venv", "build", "__pycache__", ".ruff_cache", ".pytest_cache"}
)

_TOKEN = "test-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_API_KEY = "stub-test-key-0123456789"
_SESSION_KEY = "voice-relay-test"
_SESSION_ID = "session-e2e"
_MAX_ATTACHMENT_MB = 1
_CAP_HEADROOM_BYTES = 1024 * 1024
_FIXTURE_BUDGET_BYTES = 2 * 1024 * 1024
_HTTP_TIMEOUT_S = 20.0
_DELEGATION_TIMEOUT_S = 20.0

_FIXTURE_NAMES = ("photo.png", "report.pdf", "memo.wav", "broken.pdf", "oversized.png")
_FIXTURE_MIMES = {
    "photo.png": "image/png",
    "report.pdf": "application/pdf",
    "memo.wav": "audio/wav",
    "broken.pdf": "application/pdf",
    "oversized.png": "image/png",
}


def _fixture(name: str) -> bytes:
    """Return one committed fixture, failing loudly when it is missing."""
    path = _FIXTURES_DIR / name
    assert path.is_file(), f"missing fixture {path}"
    return path.read_bytes()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _repo_snapshot() -> set[str]:
    """Return every repository file outside the ignored build/cache trees."""
    files: set[str] = set()
    for path in _REPO_ROOT.rglob("*"):
        relative = path.relative_to(_REPO_ROOT)
        if any(part in _EXCLUDED_REPO_PARTS for part in relative.parts):
            continue
        if path.is_file():
            files.add(relative.as_posix())
    return files


def _stored_files(root: Path) -> list[str]:
    """List the visible files directly under ``root`` (the inbox is flat)."""
    if not root.exists():
        return []
    return sorted(
        entry.name for entry in root.iterdir() if entry.is_file() and not entry.name.startswith(".")
    )


def _attachment_from_record(record: dict[str, Any]) -> Attachment:
    """Rebuild the attachment record the voice layer receives from the API."""
    return Attachment(
        id=str(record["id"]),
        path=Path(str(record["path"])),
        mime=str(record["mime"]),
        kind=str(record["kind"]),
        sha256=str(record["sha256"]),
        size_bytes=int(record["size_bytes"]),
        original_name=str(record["original_name"]),
        created_at=str(record["created_at"]),
        detected_mime=str(record["detected_mime"]),
        uploaded_bytes=int(record["uploaded_bytes"]),
        facts=dict(record["facts"]),
    )


def _machine_line(parts: Sequence[dict[str, object]]) -> str:
    """Return the one machine-parsable attachment line in a parts array."""
    lines = [
        str(part["text"])
        for part in parts
        if part["type"] == "text" and str(part["text"]).startswith("Attached file ")
    ]
    assert len(lines) == 1, lines
    return lines[0]


def _inline_image(parts: Sequence[dict[str, object]]) -> bytes:
    """Decode the single inline image part and return its bytes."""
    urls: list[str] = []
    for part in parts:
        if part["type"] != "image_url":
            continue
        image_url = part["image_url"]
        assert isinstance(image_url, dict)
        urls.append(str(image_url["url"]))
    assert len(urls) == 1, urls
    prefix, encoded = urls[0].split(",", 1)
    assert prefix.startswith("data:image/"), prefix
    return base64.b64decode(encoded, validate=True)


def _instruction(parts: Sequence[dict[str, object]]) -> str:
    """Flatten the text parts into the string instruction the adapter submits."""
    return "\n\n".join(str(part["text"]) for part in parts if part["type"] == "text")


def _black_and_white(payload: bytes) -> bytes:
    """Return the agent's deterministic edit of the picture it was handed."""
    with Image.open(io.BytesIO(payload)) as image:
        converted = image.convert("L")
        buffer = io.BytesIO()
        converted.save(buffer, format="PNG")
    return buffer.getvalue()


def _noise_png(width: int = 1000, height: int = 1000, seed: int = 20261102) -> bytes:
    """Return an incompressible PNG, so its wire size exceeds the 1 MB cap."""
    pixels = random.Random(seed).randbytes(width * height * 3)  # noqa: S311 - not cryptography
    image = Image.frombytes("RGB", (width, height), pixels)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class _RelayServer:
    """Run the real relay app on a loopback socket for the test session."""

    def __init__(self, app: FastAPI, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.base_url = ""

    @property
    def inbox_root(self) -> Path:
        assert self.settings.inbox_dir is not None
        return Path(self.settings.inbox_dir)

    @property
    def outbox_root(self) -> Path:
        assert self.settings.outbox_dir is not None
        return Path(self.settings.outbox_dir)

    def start(self) -> None:
        config = uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15.0
        while self.server is not None and not self.server.started:
            assert time.monotonic() < deadline, "relay server did not start"
            time.sleep(0.01)
        server = self.server
        assert server is not None and server.servers
        host, port = server.servers[0].sockets[0].getsockname()[:2]
        self.base_url = f"http://{host}:{port}"

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=15.0)


class _SpokenSink:
    """Record the terminal spoken updates the voice layer would hear."""

    def __init__(self) -> None:
        self.updates: list[SpokenUpdate] = []

    async def __call__(self, update: SpokenUpdate) -> None:
        self.updates.append(update)


@pytest.fixture(scope="module")
def relay(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_RelayServer]:
    """Start the relay once per module with storage outside the repository."""
    root = tmp_path_factory.mktemp("attachments-e2e")
    settings = Settings(
        _env_file=None,
        inbox_dir=root / "inbox",
        outbox_dir=root / "outbox",
        data_dir=root / "data",
        device_tokens_raw=_TOKEN,
        max_attachment_mb=_MAX_ATTACHMENT_MB,
        # The limiter has its own tests; this suite must not trip its burst.
        rate_limit_upload_burst=32,
        rate_limit_upload_per_minute=120,
        rate_limit_download_burst=64,
    )
    server = _RelayServer(create_app(settings), settings)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def start_stub() -> Iterator[Callable[..., HermesStub]]:
    """Start a loopback Hermes stub answering with a fixed terminal frame."""
    started: list[HermesStub] = []

    def _start(*, answer: str = "Linux", failure: str | None = None) -> HermesStub:
        stub = HermesStub(api_key=_API_KEY)
        if failure is not None:
            stub.frames = [stub.make_frame("run.failed", 1, error=failure)]
        else:
            stub.frames = [stub.make_frame("run.completed", 1, output=answer)]
        stub.start()
        started.append(stub)
        return stub

    try:
        yield _start
    finally:
        for stub in started:
            stub.stop()


async def _upload(
    client: httpx.AsyncClient,
    *,
    filename: str,
    payload: bytes,
    content_type: str,
) -> httpx.Response:
    return await client.post(
        "/v1/attachments",
        files={"file": (filename, payload, content_type)},
        headers=_AUTH,
    )


async def _accept(
    client: httpx.AsyncClient,
    *,
    filename: str,
    payload: bytes,
    content_type: str,
) -> tuple[dict[str, Any], Attachment]:
    """Upload one attachment and return the response body and stored record."""
    response = await _upload(
        client,
        filename=filename,
        payload=payload,
        content_type=content_type,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body, _attachment_from_record(body["attachment"])


def test_image_edit_path_end_to_end(
    relay: _RelayServer,
    start_stub: Callable[..., HermesStub],
) -> None:
    """Upload a photo, delegate an edit, then download the edited artifact."""

    async def scenario() -> None:
        answer = "Saved the black-and-white version as edits/photo-bw.png."
        stub = start_stub(answer=answer)
        hermes = HermesClient(
            stub.base_url,
            _API_KEY,
            session_key=_SESSION_KEY,
            timeout_s=5.0,
            connect_timeout_s=2.0,
            stream_timeout_s=_DELEGATION_TIMEOUT_S,
            reconnect_backoff_s=0.01,
            max_reconnect_backoff_s=0.05,
        )
        sink = _SpokenSink()
        service = DelegationService(
            hermes,
            sink,
            session_id=_SESSION_ID,
            session_key=_SESSION_KEY,
        )
        repo_before = _repo_snapshot()
        try:
            async with httpx.AsyncClient(
                base_url=relay.base_url,
                timeout=httpx.Timeout(_HTTP_TIMEOUT_S),
            ) as http:
                body, attachment = await _accept(
                    http,
                    filename="photo.png",
                    payload=_fixture("photo.png"),
                    content_type="image/png",
                )
                assert body["spoken"] == "Picture saved."
                assert attachment.kind == "image"
                assert attachment.path.is_relative_to(relay.inbox_root.resolve())
                stored = attachment.path.read_bytes()
                assert _sha256(stored) == attachment.sha256
                assert attachment.size_bytes == len(stored)
                with Image.open(io.BytesIO(stored)) as image:
                    assert image.format == "PNG"
                    assert image.size == (64, 48)
                    # Ingest re-encoded the picture, so the fixture EXIF is gone.
                    assert not dict(image.getexif())

                utterance = "Make the attached picture black and white."
                parts = delegation_input(utterance, [attachment], InboxStore(relay.settings))
                assert parts[0] == {"type": "text", "text": utterance}
                line = _machine_line(parts)
                assert f"path={attachment.path}" in line
                assert f"sha256={attachment.sha256}" in line
                assert "mime=image/png" in line
                assert f"bytes={attachment.size_bytes}" in line
                assert "name=photo.png" in line
                assert "pixels=3072" in line
                inline = _inline_image(parts)
                assert inline == stored

                # The agent's tool writes the edit into the outbox; the relay
                # itself never writes there.
                edited = _black_and_white(inline)
                artifact_id = "edits/photo-bw.png"
                artifact_path = relay.outbox_root / artifact_id
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_bytes(edited)

                handle = await service.delegate(
                    _instruction(parts),
                    idempotency_key="e2e-image-edit",
                )
                assert handle.run_id == stub.run_id
                snapshot = await service.wait(
                    handle.delegation_id,
                    timeout_s=_DELEGATION_TIMEOUT_S,
                )
                assert snapshot.state is DelegationState.COMPLETED
                assert snapshot.deliveries == 1
                assert len(sink.updates) == 1
                update = sink.updates[0]
                assert update.kind == "answer"
                assert update.final is True
                assert update.text == answer

                submits = [
                    request
                    for request in stub.requests
                    if request["method"] == "POST" and request["path"] == "/v1/runs"
                ]
                assert len(submits) == 1
                submitted = submits[0]["json"]["input"]
                assert submitted.startswith(utterance)
                assert f"sha256={attachment.sha256}" in submitted
                assert f"path={attachment.path}" in submitted

                listing = await http.get("/v1/artifacts", headers=_AUTH)
                assert listing.status_code == 200
                payload = listing.json()
                ids = [artifact["id"] for artifact in payload["artifacts"]]
                assert artifact_id in ids
                entry = next(
                    artifact for artifact in payload["artifacts"] if artifact["id"] == artifact_id
                )
                assert entry["sha256"] == _sha256(edited)
                assert entry["mime"] == "image/png"
                assert entry["size_bytes"] == len(edited)
                # No absolute path ever leaves the relay in a listing.
                assert "path" not in entry
                for value in entry.values():
                    if isinstance(value, str):
                        assert not value.startswith("/")

                download = await http.get(f"/v1/artifacts/{artifact_id}", headers=_AUTH)
                assert download.status_code == 200
                assert download.content == edited
                assert download.headers["content-type"] == "image/png"
                assert download.headers["x-artifact-sha256"] == _sha256(edited)
                assert download.headers["etag"] == f'"{_sha256(edited)}"'
                assert download.headers["content-disposition"].startswith("attachment;")
                assert download.headers["x-content-type-options"] == "nosniff"
                assert int(download.headers["content-length"]) == len(edited)
                with Image.open(io.BytesIO(download.content)) as image:
                    assert image.mode == "L"
                    assert image.size == (64, 48)

                conditional = await http.get(
                    f"/v1/artifacts/{artifact_id}",
                    headers={**_AUTH, "If-None-Match": download.headers["etag"]},
                )
                assert conditional.status_code == 304
                assert conditional.content == b""

            assert _repo_snapshot() == repo_before
        finally:
            await service.aclose()
            await hermes.aclose()

    asyncio.run(scenario())


def test_document_analysis_path_end_to_end(
    relay: _RelayServer,
    start_stub: Callable[..., HermesStub],
) -> None:
    """Upload a PDF, delegate an analysis with an excerpt, download the summary."""

    async def scenario() -> None:
        answer = "The report says units shipped were 42, region North."
        stub = start_stub(answer=answer)
        hermes = HermesClient(
            stub.base_url,
            _API_KEY,
            session_key=_SESSION_KEY,
            timeout_s=5.0,
            connect_timeout_s=2.0,
            stream_timeout_s=_DELEGATION_TIMEOUT_S,
            reconnect_backoff_s=0.01,
            max_reconnect_backoff_s=0.05,
        )
        sink = _SpokenSink()
        service = DelegationService(
            hermes,
            sink,
            session_id=_SESSION_ID,
            session_key=_SESSION_KEY,
        )
        try:
            async with httpx.AsyncClient(
                base_url=relay.base_url,
                timeout=httpx.Timeout(_HTTP_TIMEOUT_S),
            ) as http:
                body, attachment = await _accept(
                    http,
                    filename="report.pdf",
                    payload=_fixture("report.pdf"),
                    content_type="application/pdf",
                )
                assert body["spoken"] == "Document saved."
                assert attachment.kind == "document"
                assert attachment.facts.get("pages") == 1
                stored = attachment.path.read_bytes()
                assert _sha256(stored) == attachment.sha256
                extracted = PdfReader(io.BytesIO(stored)).pages[0].extract_text()
                assert "Units shipped: 42" in extracted

                parts = delegation_input(
                    "Summarise the attached report.",
                    [attachment],
                    InboxStore(relay.settings),
                    excerpts={attachment.id: extracted},
                )
                line = _machine_line(parts)
                assert f"sha256={attachment.sha256}" in line
                assert "mime=application/pdf" in line
                assert "pages=1" in line
                envelopes = [
                    str(part["text"])
                    for part in parts
                    if part["type"] == "text" and ENVELOPE_PREFIX in str(part["text"])
                ]
                assert len(envelopes) == 1
                assert "Units shipped: 42" in envelopes[0]
                assert f"sha256={attachment.sha256}" in envelopes[0]
                assert not any(part["type"] == "image_url" for part in parts)

                summary = f"Summary\n\n{answer}\n"
                artifact_id = "notes/report-summary.md"
                artifact_path = relay.outbox_root / artifact_id
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_text(summary, encoding="utf-8")

                handle = await service.delegate(
                    _instruction(parts),
                    idempotency_key="e2e-document-analysis",
                )
                snapshot = await service.wait(
                    handle.delegation_id,
                    timeout_s=_DELEGATION_TIMEOUT_S,
                )
                assert snapshot.state is DelegationState.COMPLETED
                assert snapshot.deliveries == 1
                assert len(sink.updates) == 1
                assert sink.updates[0].kind == "answer"
                assert sink.updates[0].text == answer

                submits = [
                    request
                    for request in stub.requests
                    if request["method"] == "POST" and request["path"] == "/v1/runs"
                ]
                assert len(submits) == 1
                submitted = submits[0]["json"]["input"]
                assert "Units shipped: 42" in submitted
                assert f"path={attachment.path}" in submitted

                download = await http.get(f"/v1/artifacts/{artifact_id}", headers=_AUTH)
                assert download.status_code == 200
                assert download.text == summary
                assert download.headers["content-type"] == "text/markdown"
                assert download.headers["x-artifact-sha256"] == _sha256(summary.encode())
                assert int(download.headers["content-length"]) == len(summary.encode())
        finally:
            await service.aclose()
            await hermes.aclose()

    asyncio.run(scenario())


def test_audio_upload_reaches_delegation(
    relay: _RelayServer,
    start_stub: Callable[..., HermesStub],
) -> None:
    """Upload a short WAV and delegate it as a path-only attachment."""

    async def scenario() -> None:
        answer = "The memo is a short tone."
        stub = start_stub(answer=answer)
        hermes = HermesClient(
            stub.base_url,
            _API_KEY,
            session_key=_SESSION_KEY,
            timeout_s=5.0,
            connect_timeout_s=2.0,
            stream_timeout_s=_DELEGATION_TIMEOUT_S,
            reconnect_backoff_s=0.01,
            max_reconnect_backoff_s=0.05,
        )
        sink = _SpokenSink()
        service = DelegationService(
            hermes,
            sink,
            session_id=_SESSION_ID,
            session_key=_SESSION_KEY,
        )
        try:
            async with httpx.AsyncClient(
                base_url=relay.base_url,
                timeout=httpx.Timeout(_HTTP_TIMEOUT_S),
            ) as http:
                body, attachment = await _accept(
                    http,
                    filename="memo.wav",
                    payload=_fixture("memo.wav"),
                    content_type="audio/wav",
                )
                assert body["spoken"] == "Audio saved."
                assert attachment.kind == "audio"
                assert attachment.mime == "audio/wav"
                with wave.open(io.BytesIO(attachment.path.read_bytes())) as handle:
                    assert handle.getnchannels() == 1
                    assert handle.getsampwidth() == 2
                    assert handle.getframerate() == 16_000
                    assert handle.getnframes() == 4000

                parts = delegation_input(
                    "Transcribe the attached memo.",
                    [attachment],
                    InboxStore(relay.settings),
                )
                assert [part["type"] for part in parts] == ["text", "text"]
                line = _machine_line(parts)
                assert "mime=audio/wav" in line
                assert f"sha256={attachment.sha256}" in line
                assert not any(part["type"] == "image_url" for part in parts)

                handle = await service.delegate(
                    _instruction(parts),
                    idempotency_key="e2e-audio-memo",
                )
                snapshot = await service.wait(
                    handle.delegation_id,
                    timeout_s=_DELEGATION_TIMEOUT_S,
                )
                assert snapshot.state is DelegationState.COMPLETED
                assert snapshot.deliveries == 1
                assert len(sink.updates) == 1
                assert sink.updates[0].text == answer

                submits = [
                    request
                    for request in stub.requests
                    if request["method"] == "POST" and request["path"] == "/v1/runs"
                ]
                assert len(submits) == 1
                assert f"sha256={attachment.sha256}" in submits[0]["json"]["input"]
        finally:
            await service.aclose()
            await hermes.aclose()

    asyncio.run(scenario())


def test_malformed_pdf_is_rejected_with_spoken_error(relay: _RelayServer) -> None:
    """A truncated PDF is refused with a spoken sentence and stores nothing."""

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url=relay.base_url,
            timeout=httpx.Timeout(_HTTP_TIMEOUT_S),
        ) as http:
            before = _stored_files(relay.inbox_root)
            response = await _upload(
                http,
                filename="broken.pdf",
                payload=_fixture("broken.pdf"),
                content_type="application/pdf",
            )
            assert response.status_code == 422
            body = response.json()
            assert body["error"] == "corrupt_document"
            assert body["spoken"] == CorruptDocumentError.spoken
            assert body["detail"]
            assert _stored_files(relay.inbox_root) == before

    asyncio.run(scenario())


def test_oversized_image_is_rejected_with_spoken_error(relay: _RelayServer) -> None:
    """A 100 MP picture is refused before the relay decodes it."""

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url=relay.base_url,
            timeout=httpx.Timeout(_HTTP_TIMEOUT_S),
        ) as http:
            before = _stored_files(relay.inbox_root)
            response = await _upload(
                http,
                filename="oversized.png",
                payload=_fixture("oversized.png"),
                content_type="image/png",
            )
            assert response.status_code == 413
            body = response.json()
            assert body["error"] == "image_too_large"
            assert body["spoken"] == ImageTooLargeError.spoken
            assert _stored_files(relay.inbox_root) == before

    asyncio.run(scenario())


def test_oversized_wire_image_is_rejected_with_spoken_error(relay: _RelayServer) -> None:
    """A picture whose bytes exceed the upload cap is refused by length."""

    async def scenario() -> None:
        payload = _noise_png()
        assert len(payload) > _MAX_ATTACHMENT_MB * 1024 * 1024 + _CAP_HEADROOM_BYTES
        async with httpx.AsyncClient(
            base_url=relay.base_url,
            timeout=httpx.Timeout(_HTTP_TIMEOUT_S),
        ) as http:
            before = _stored_files(relay.inbox_root)
            response = await _upload(
                http,
                filename="large.png",
                payload=payload,
                content_type="image/png",
            )
            assert response.status_code == 413
            body = response.json()
            assert body["error"] == "file_too_large"
            assert body["spoken"] == FileTooLargeError.spoken
            assert body["retry_after"] is None or body["retry_after"] >= 0
            assert _stored_files(relay.inbox_root) == before

    asyncio.run(scenario())


def test_artifact_failures_are_spoken(relay: _RelayServer) -> None:
    """Unknown ids and missing credentials are refused with spoken sentences."""

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url=relay.base_url,
            timeout=httpx.Timeout(_HTTP_TIMEOUT_S),
        ) as http:
            missing = await http.get("/v1/artifacts/edits/absent.png", headers=_AUTH)
            assert missing.status_code == 404
            body = missing.json()
            assert body["error"] == "artifact_not_found"
            assert body["spoken"] == ArtifactNotFoundError.spoken

            unauthorized = await http.get("/v1/artifacts")
            assert unauthorized.status_code == 401
            assert unauthorized.json()["error"] == "unauthorized"
            assert unauthorized.json()["reason"] == "missing_credentials"

    asyncio.run(scenario())


def test_delegation_failure_is_spoken(
    relay: _RelayServer,
    start_stub: Callable[..., HermesStub],
) -> None:
    """A failed run still reaches the caller once, as a spoken failure."""

    async def scenario() -> None:
        stub = start_stub(failure="The turn did not complete.")
        hermes = HermesClient(
            stub.base_url,
            _API_KEY,
            session_key=_SESSION_KEY,
            timeout_s=5.0,
            connect_timeout_s=2.0,
            stream_timeout_s=_DELEGATION_TIMEOUT_S,
            reconnect_backoff_s=0.01,
            max_reconnect_backoff_s=0.05,
        )
        sink = _SpokenSink()
        service = DelegationService(
            hermes,
            sink,
            session_id=_SESSION_ID,
            session_key=_SESSION_KEY,
        )
        try:
            async with httpx.AsyncClient(
                base_url=relay.base_url,
                timeout=httpx.Timeout(_HTTP_TIMEOUT_S),
            ) as http:
                _, attachment = await _accept(
                    http,
                    filename="report.pdf",
                    payload=_fixture("report.pdf"),
                    content_type="application/pdf",
                )
                parts = delegation_input(
                    "Summarise the attached report.",
                    [attachment],
                    InboxStore(relay.settings),
                )
                handle = await service.delegate(
                    _instruction(parts),
                    idempotency_key="e2e-delegation-failure",
                )
                snapshot = await service.wait(
                    handle.delegation_id,
                    timeout_s=_DELEGATION_TIMEOUT_S,
                )
                assert snapshot.state is DelegationState.FAILED
                assert snapshot.deliveries == 1
                assert len(sink.updates) == 1
                update = sink.updates[0]
                assert update.kind == "failure"
                assert update.final is True
                assert update.text == delegation_failure()
        finally:
            await service.aclose()
            await hermes.aclose()

    asyncio.run(scenario())


def test_end_to_end_stays_on_loopback(
    relay: _RelayServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every socket the upload path opens is a loopback connection."""

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url=relay.base_url,
            timeout=httpx.Timeout(_HTTP_TIMEOUT_S),
        ) as http:
            _, attachment = await _accept(
                http,
                filename="photo.png",
                payload=_fixture("photo.png"),
                content_type="image/png",
            )
            assert attachment.path.is_relative_to(relay.inbox_root.resolve())
            listing = await http.get("/v1/artifacts", headers=_AUTH)
            assert listing.status_code == 200

    addresses: list[Any] = []
    original_connect = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> Any:
        addresses.append(address)
        return original_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    asyncio.run(scenario())
    assert addresses
    for address in addresses:
        assert address[0] == "127.0.0.1", address


def test_fixture_set_is_small_and_well_formed() -> None:
    """The committed fixtures are synthetic, intact, and under the 2 MB budget."""

    total = 0
    for name in _FIXTURE_NAMES:
        path = _FIXTURES_DIR / name
        assert path.is_file(), name
        payload = path.read_bytes()
        total += len(payload)
        assert sniff_mime(payload) == _FIXTURE_MIMES[name], name
    assert total < _FIXTURE_BUDGET_BYTES

    with Image.open(io.BytesIO(_fixture("photo.png"))) as photo:
        assert photo.format == "PNG"
        assert photo.size == (64, 48)
        assert dict(photo.getexif())

    report = _fixture("report.pdf")
    reader = PdfReader(io.BytesIO(report))
    assert len(reader.pages) == 1
    assert "Units shipped: 42" in reader.pages[0].extract_text()

    broken = _fixture("broken.pdf")
    assert report.startswith(broken)
    with pytest.raises(Exception):  # noqa: B017 - any reader failure means corrupt
        PdfReader(io.BytesIO(broken)).pages[0].extract_text()

    with wave.open(io.BytesIO(_fixture("memo.wav"))) as memo:
        assert memo.getnchannels() == 1
        assert memo.getsampwidth() == 2
        assert memo.getframerate() == 16_000
        assert memo.getnframes() == 4000

    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        with Image.open(io.BytesIO(_fixture("oversized.png"))) as huge:
            assert huge.size == (10_000, 10_000)
            assert huge.mode == "1"
            assert huge.size[0] * huge.size[1] == 100_000_000
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit
