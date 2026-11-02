"""Offline integration tests for the text relay's Hermes run path.

The fixtures in ``relay/tests/fixtures/*.jsonl`` are synthetic transcripts
shaped exactly like the ones recorded against a real gateway in
``docs/research/hermes-api.md`` (§4 event stream, §6 approvals, §7 clarify,
§8 stop).  A loopback FastAPI stub replays them over real HTTP/SSE, so every
path here runs offline: no Hermes gateway, no model call, no network beyond
``127.0.0.1``.

The contract tests at the bottom fail when the documented event vocabulary,
the decoder's name mapping or the fixture schema drift apart.  They parse the
contract document itself, so a new event type cannot be added to the docs
without a fixture and a decoder mapping, and a decoder change cannot land
without the docs.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from relay.hermes.client import (
    HermesAuthError,
    HermesClient,
    HermesConflictError,
    HermesError,
    HermesProtocolError,
    HermesRunGoneError,
    HermesStreamError,
)
from relay.hermes.events import TERMINAL_KINDS, EventKind, SseFrame, decode_event

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONTRACT_DOC = _REPO_ROOT / "docs" / "research" / "hermes-api.md"

_API_KEY = "stub-test-key-0123456789"
_SESSION_KEY = "voice-relay-test"
_RUN_ID = "run_fixture00000000000000000000000001"
_MAX_FIXTURE_BYTES = 2 * 1024 * 1024
_TERMINAL_NAMES = frozenset({"run.completed", "run.cancelled", "run.failed"})
_STATUS_BY_EVENT = {
    "run.completed": "completed",
    "run.cancelled": "cancelled",
    "run.failed": "failed",
}
_ENVELOPE_KEYS = frozenset({"event", "seq", "run_id", "timestamp"})


def _load_fixture(name: str) -> list[dict[str, Any]]:
    path = _FIXTURES_DIR / name
    entries: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        entry = json.loads(line)
        if not isinstance(entry, dict):
            raise ValueError(f"{path.name}:{number} is not a JSON object")
        entries.append(entry)
    return entries


def _render(frame: Mapping[str, Any], run_id: str) -> str:
    data = dict(frame["data"])
    data["run_id"] = run_id
    payload = json.dumps(data, separators=(",", ":"))
    return f"id: {frame['id']}\ndata: {payload}\n\n"


def _event_payload(event: Any, seq: Any, **payload: Any) -> dict[str, Any]:
    offset = seq if isinstance(seq, int) and not isinstance(seq, bool) else 0
    return {
        "event": event,
        "run_id": _RUN_ID,
        "timestamp": 1793616000.0 + offset,
        "seq": seq,
        **payload,
    }


def _event_frame(event: str, seq: int, **payload: Any) -> dict[str, Any]:
    return {"id": seq, "data": _event_payload(event, seq, **payload)}


def _raw_sse(payload: Mapping[str, Any], *, seq: int | None = None) -> str:
    prefix = "" if seq is None else f"id: {seq}\n"
    return f"{prefix}data: {json.dumps(payload)}\n\n"


def _error(message: str, code: str | None, status_code: int) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": None,
                "code": code,
            }
        },
        status_code=status_code,
    )


def _auth_error() -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "message": "Invalid gateway API key (API_SERVER_KEY)",
                "type": "gateway_auth_error",
                "code": "gateway_auth_failed",
            }
        },
        status_code=401,
    )


class FixtureGateway:
    """In-process Hermes API double that replays one recorded SSE fixture.

    Only the surface the run path needs is implemented: admission, the event
    stream, run status and stop.  Approval and clarify *answers* are the
    approval gate's business; here the prompts are only read from the stream
    and from ``pending_controls``.
    """

    def __init__(
        self,
        fixture: str | None = None,
        *,
        api_key: str = _API_KEY,
        run_id: str = _RUN_ID,
    ) -> None:
        self.api_key = api_key
        self.run_id = run_id
        self.frames = _load_fixture(fixture) if fixture is not None else []
        self.raw_frames: list[str] | None = None
        self.hold_open = False
        self.keepalive_every_s: float | None = None
        self.events_gone = False
        self.plain_text_error: str | None = None
        self.foreign_sessions: set[str] = set()
        self.initial_status = "running"
        self.initial_output = ""
        self.pending_controls: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.runs: dict[str, dict[str, Any]] = {}
        self.idempotency: dict[str, str] = {}
        self.app = self._build_app()
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.base_url = ""

    def start(self) -> None:
        """Start the stub server and wait until it is accepting requests."""
        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=0,
            log_level="error",
            lifespan="off",
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()

        deadline = time.monotonic() + 5.0
        while self.server is not None and not self.server.started:
            if time.monotonic() > deadline:
                msg = "stub Hermes server did not start"
                raise RuntimeError(msg)
            time.sleep(0.01)

        server = self.server
        if server is None or not server.servers:
            msg = "stub Hermes server has no listening socket"
            raise RuntimeError(msg)
        sock = server.servers[0].sockets[0]
        host, port = sock.getsockname()[:2]
        self.base_url = f"http://{host}:{port}"

    def stop(self) -> None:
        """Ask the stub server to exit and wait for its thread."""
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=5.0)

    def _authorized(self, request: Request) -> bool:
        return request.headers.get("authorization") == f"Bearer {self.api_key}"

    def _record(self, request: Request, body: Any = None) -> None:
        self.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "headers": dict(request.headers),
                "json": body,
            }
        )

    def _new_run(self, session_id: str | None) -> dict[str, Any]:
        return {
            "status": self.initial_status,
            "session_id": session_id or self.run_id,
            "admission_id": "adm_stub00000000000000000000000001",
            "execution_generation": 1,
            "output": self.initial_output,
            "pending_controls": list(self.pending_controls),
            "stop_event": asyncio.Event(),
            "cancelled_emitted": False,
        }

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/v1/runs")
        async def submit_run(request: Request) -> JSONResponse:
            if not self._authorized(request):
                return _auth_error()
            try:
                body = await request.json()
            except ValueError:
                return _error("Invalid JSON", None, 400)
            self._record(request, body)
            if not isinstance(body, dict):
                return _error("Invalid JSON", None, 400)
            if not body.get("input"):
                return _error("Missing 'input' field", None, 400)
            session_id = body.get("session_id")
            if session_id in self.foreign_sessions:
                return _error("permission_denied", "permission_denied", 409)

            headers: dict[str, str] = {}
            session_key = request.headers.get("x-hermes-session-key")
            if session_key is not None:
                headers["X-Hermes-Session-Key"] = session_key

            idempotency_key = request.headers.get("idempotency-key")
            if idempotency_key is not None:
                canonical = json.dumps(body, sort_keys=True)
                previous = self.idempotency.get(idempotency_key)
                if previous is not None and previous != canonical:
                    return _error(
                        "Idempotency-Key was already used with a different request payload",
                        "idempotency_key_conflict",
                        409,
                    )
                if previous is not None:
                    run = self.runs.get(self.run_id)
                    status = run["status"] if run is not None else "started"
                    return JSONResponse(
                        {"run_id": self.run_id, "status": status, "replayed": True},
                        status_code=202,
                        headers=headers,
                    )
                self.idempotency[idempotency_key] = canonical

            self.runs[self.run_id] = self._new_run(session_id)
            return JSONResponse(
                {"run_id": self.run_id, "status": "started", "replayed": False},
                status_code=202,
                headers=headers,
            )

        @app.get("/v1/runs/{run_id}/events")
        async def run_events(run_id: str, request: Request) -> Any:
            if not self._authorized(request):
                return _auth_error()
            if self.plain_text_error is not None:
                return PlainTextResponse(self.plain_text_error, status_code=500)

            last_seq_raw = request.query_params.get("last_seq")
            last_event_id = request.headers.get("last-event-id")
            self._record(request)
            self.requests[-1]["last_seq"] = last_seq_raw
            self.requests[-1]["last_event_id"] = last_event_id

            if self.events_gone:
                return _error(f"Run not found: {run_id}", "run_not_found", 404)
            run = self.runs.get(run_id)
            if run is None:
                return _error(f"Run not found: {run_id}", "run_not_found", 404)

            resume = self._resume_point(last_seq_raw, last_event_id)
            return StreamingResponse(
                self._stream(run, run_id, resume),
                media_type="text/event-stream",
            )

        @app.get("/v1/runs/{run_id}")
        async def get_run(run_id: str, request: Request) -> JSONResponse:
            if not self._authorized(request):
                return _auth_error()
            self._record(request)
            run = self.runs.get(run_id)
            if run is None:
                return _error(f"Run not found: {run_id}", "run_not_found", 404)
            return JSONResponse(
                {
                    "object": "hermes.run",
                    "model": "hermes-agent",
                    "runtime": {"provider": "", "model": "", "route_source": "global"},
                    "created_at": 1793615952.0,
                    "pending_controls": run["pending_controls"],
                    "run_id": run_id,
                    "status": run["status"],
                    "session_id": run["session_id"],
                    "admission_id": run["admission_id"],
                    "execution_generation": run["execution_generation"],
                    "output": run["output"],
                    "usage": {},
                }
            )

        @app.post("/v1/runs/{run_id}/stop")
        async def stop_run(run_id: str, request: Request) -> JSONResponse:
            if not self._authorized(request):
                return _auth_error()
            self._record(request, {})
            run = self.runs.get(run_id)
            if run is None:
                return _error(f"Run not found: {run_id}", "run_not_found", 404)

            if run["status"] in _STATUS_BY_EVENT.values():
                status = run["status"]
            else:
                run["status"] = "cancelled"
                run["stop_event"].set()
                status = "stopping"
            return JSONResponse(
                {
                    "run_id": run_id,
                    "status": status,
                    "admission_id": run["admission_id"],
                }
            )

        return app

    async def _stream(self, run: dict[str, Any], run_id: str, resume: int) -> AsyncIterator[str]:
        yield ": open\n\n"

        if self.raw_frames is not None:
            for raw in self.raw_frames:
                yield raw
            return

        emitted = resume
        for frame in self.frames:
            if "comment" in frame:
                yield f": {frame['comment']}\n\n"
                continue
            seq = frame.get("id")
            if not isinstance(seq, int) or seq <= resume:
                continue
            data = frame["data"]
            if self.hold_open and data.get("event") in _TERMINAL_NAMES:
                continue
            yield _render(frame, run_id)
            emitted = max(emitted, seq)
            if data.get("event") in _TERMINAL_NAMES:
                self._settle(run, frame)
                yield ": stream closed\n\n"
                return

        if not self.hold_open:
            return

        while not run["stop_event"].is_set():
            if self.keepalive_every_s is None:
                await run["stop_event"].wait()
                break
            try:
                await asyncio.wait_for(
                    run["stop_event"].wait(),
                    timeout=self.keepalive_every_s,
                )
            except TimeoutError:
                yield ": keepalive\n\n"

        if run["cancelled_emitted"]:
            return
        run["cancelled_emitted"] = True
        next_seq = emitted + 1
        cancelled = {
            "id": next_seq,
            "data": {
                "event": "run.cancelled",
                "run_id": run_id,
                "timestamp": time.time(),
                "completed": False,
                "partial": False,
                "interrupted": True,
                "turn_exit_reason": "interrupted_by_user",
                "seq": next_seq,
            },
        }
        yield _render(cancelled, run_id)
        self._settle(run, cancelled)
        yield ": stream closed\n\n"

    @staticmethod
    def _resume_point(last_seq_raw: str | None, last_event_id: str | None) -> int:
        for raw in (last_seq_raw, last_event_id):
            if raw is None:
                continue
            try:
                return int(raw)
            except ValueError:
                continue
        return -1

    @staticmethod
    def _settle(run: dict[str, Any], frame: Mapping[str, Any]) -> None:
        event = frame["data"].get("event")
        status = _STATUS_BY_EVENT.get(event)
        if status is not None:
            run["status"] = status
        run["output"] = str(frame["data"].get("output", ""))


@pytest.fixture
def gateway() -> Iterator[Callable[..., FixtureGateway]]:
    servers: list[FixtureGateway] = []

    def _make(fixture: str | None = None, **knobs: Any) -> FixtureGateway:
        server = FixtureGateway(fixture)
        for name, value in knobs.items():
            setattr(server, name, value)
        server.start()
        servers.append(server)
        return server

    yield _make
    for server in servers:
        server.stop()


def _client(stub: FixtureGateway, **overrides: Any) -> HermesClient:
    return HermesClient(
        stub.base_url,
        _API_KEY,
        session_key=_SESSION_KEY,
        timeout_s=2.0,
        connect_timeout_s=1.0,
        stop_timeout_s=2.0,
        stream_timeout_s=2.0,
        reconnect_backoff_s=0.01,
        max_reconnect_backoff_s=0.05,
        **overrides,
    )


async def _collect(events: AsyncIterator[Any]) -> list[Any]:
    return [event async for event in events]


# ---------------------------------------------------------------------------
# Path tests: success, cancellation, error and malformed-event handling
# ---------------------------------------------------------------------------


def test_success_path_replays_recorded_tool_turn(gateway: Callable[..., FixtureGateway]) -> None:
    stub = gateway("hermes_run_tool_turn.jsonl")
    assert stub.base_url.startswith("http://127.0.0.1:")

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("Use the terminal tool to run uname -s")
            assert admission.run_id == stub.run_id
            assert admission.status == "started"
            assert admission.replayed is False
            assert admission.session_key == _SESSION_KEY

            events = await _collect(client.events(admission.run_id))
            assert [event.kind for event in events] == [
                EventKind.TOOL_STARTED,
                EventKind.TOOL_COMPLETED,
                EventKind.MESSAGE_INTERIM,
                EventKind.RUN_COMPLETED,
            ]
            assert [event.seq for event in events] == [0, 1, 2, 3]
            assert events[0].tool == "terminal"
            assert events[0].tool_call_id == "call_305c3106aab2477a9b0c1f1e"
            assert events[1].payload["duration"] == 0
            assert events[1].payload["error"] is False
            text = "".join(
                event.text_delta or ""
                for event in events
                if event.kind is EventKind.MESSAGE_INTERIM
            )
            assert text.strip() == "Linux"
            assert events[-1].output == "Linux"
            assert events[-1].completed is True
            assert sum(1 for event in events if event.terminal) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_approval_turn_streams_prompt_then_settles_in_documented_order(
    gateway: Callable[..., FixtureGateway],
) -> None:
    stub = gateway("hermes_run_approval.jsonl")

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("delete the fixture directory")
            events = await _collect(client.events(admission.run_id))
            assert [event.kind for event in events] == [
                EventKind.TOOL_STARTED,
                EventKind.APPROVAL_REQUEST,
                EventKind.APPROVAL_SETTLED,
                EventKind.TOOL_COMPLETED,
                EventKind.MESSAGE_INTERIM,
                EventKind.RUN_COMPLETED,
            ]
            request = events[1]
            assert request.payload["kind"] == "approval"
            assert request.prompt_id == "a8ff8ef17ed04d718fcd6bc9e4cd3210"
            assert request.execution_generation == 1
            assert request.payload["command"] == "rm -rf /tmp/hermes-talk-fixture-dir"
            assert request.payload["description"] == "delete in temp path"
            assert request.payload["choices"] == ["once", "deny", "session", "always"]

            settled = events[2]
            assert settled.prompt_id == request.prompt_id
            assert settled.execution_generation == 1
            assert settled.terminal is False

            assert events[3].tool == "terminal"
            assert events[3].payload["error"] is False
            assert events[-1].output == "done"
            assert sum(1 for event in events if event.terminal) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_clarify_turn_streams_question_and_status_keeps_run_running(
    gateway: Callable[..., FixtureGateway],
) -> None:
    stub = gateway(
        "hermes_run_clarify.jsonl",
        pending_controls=[
            {
                "kind": "clarify",
                "prompt_id": "31fd319393",
                "execution_generation": 1,
                "question": (
                    'Which colour do you prefer, red or blue?\nReply "skip" to skip this question.'
                ),
                "choices": ["Red (Recommended)", "Blue"],
                "multi_select": False,
            }
        ],
    )

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("ask which colour")
            status = await client.get_run(admission.run_id)
            assert status.status == "running"
            assert len(status.pending_controls) == 1
            control = status.pending_controls[0]
            assert control["kind"] == "clarify"
            assert control["prompt_id"] == "31fd319393"
            assert control["multi_select"] is False
            assert control["choices"] == ["Red (Recommended)", "Blue"]

            events = await _collect(client.events(admission.run_id))
            assert [event.kind for event in events] == [
                EventKind.TOOL_STARTED,
                EventKind.CLARIFY_REQUEST,
                EventKind.TOOL_COMPLETED,
                EventKind.MESSAGE_INTERIM,
                EventKind.RUN_COMPLETED,
            ]
            request = events[1]
            assert request.payload["kind"] == "clarify"
            assert request.prompt_id == "31fd319393"
            assert request.execution_generation == 1
            assert request.payload["multi_select"] is False
            assert request.payload["choices"] == ["Red (Recommended)", "Blue"]
            assert "skip" in request.payload["question"]
            assert events[-1].output == "Red"
            assert sum(1 for event in events if event.terminal) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_failed_run_streams_error_as_single_terminal_event(
    gateway: Callable[..., FixtureGateway],
) -> None:
    stub = gateway("hermes_run_failed.jsonl")

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("turn that dies")
            events = await _collect(client.events(admission.run_id))
            assert [event.kind for event in events] == [EventKind.RUN_FAILED]
            failed = events[0]
            assert failed.terminal is True
            assert failed.completed is False
            assert failed.partial is False
            assert failed.interrupted is False
            assert failed.error_message == "The turn did not complete.\nUse /retry to try again."
            assert failed.output is None
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_cancelled_run_is_a_user_stop_not_a_failure(
    gateway: Callable[..., FixtureGateway],
) -> None:
    stub = gateway("hermes_run_cancelled.jsonl")

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("stop before answering")
            events = await _collect(client.events(admission.run_id))
            assert [event.kind for event in events] == [EventKind.RUN_CANCELLED]
            cancelled = events[0]
            assert cancelled.terminal is True
            assert cancelled.interrupted is True
            assert cancelled.completed is False
            assert cancelled.partial is False
            assert cancelled.turn_exit_reason == "interrupted_during_api_call"
            assert cancelled.error_message is None
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_replay_truncated_is_surfaced_and_stream_continues_to_terminal(
    gateway: Callable[..., FixtureGateway],
) -> None:
    stub = gateway("hermes_run_replay_truncated.jsonl")

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("resume an old run")
            events = await _collect(client.events(admission.run_id, last_seq=3))
            assert [event.kind for event in events] == [
                EventKind.REPLAY_TRUNCATED,
                EventKind.MESSAGE_INTERIM,
                EventKind.RUN_COMPLETED,
            ]
            truncated = events[0]
            assert truncated.payload["oldest_retained_seq"] == 40
            assert truncated.payload["requested_seq"] == 3
            assert truncated.terminal is False
            assert events[-1].output == "Linux"
            assert sum(1 for event in events if event.terminal) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_stop_on_live_stream_yields_cancelled_terminal_promptly(
    gateway: Callable[..., FixtureGateway],
) -> None:
    stub = gateway("hermes_run_tool_turn.jsonl", hold_open=True, keepalive_every_s=0.02)

    async def scenario() -> None:
        client = _client(stub)
        observed: list[Any] = []

        async def consume() -> None:
            async for event in client.events(admission.run_id):
                observed.append(event)

        admission = await client.submit_run("wait for the stop")
        task = asyncio.create_task(consume())
        try:
            await asyncio.sleep(0.05)
            stop_started = time.monotonic()
            result = await client.stop(admission.run_id)
            assert result.status == "stopping"
            assert result.elapsed_s < 0.5

            await asyncio.wait_for(task, timeout=1.0)
            assert time.monotonic() - stop_started < 0.5
            cancelled = [event for event in observed if event.kind is EventKind.RUN_CANCELLED]
            assert len(cancelled) == 1
            assert cancelled[0].interrupted is True
            assert cancelled[0].turn_exit_reason == "interrupted_by_user"
            assert sum(1 for event in observed if event.terminal) == 1

            second = await client.stop(admission.run_id)
            assert second.status == "cancelled"
        finally:
            if not task.done():
                task.cancel()
            await client.aclose()

    asyncio.run(scenario())


def test_comment_frames_are_ignored_and_events_still_decode(
    gateway: Callable[..., FixtureGateway],
) -> None:
    stub = gateway(
        raw_frames=[
            ": open\n\n",
            ": keepalive\n\n",
            _raw_sse(_event_payload("message.delta", 0, delta="x"), seq=0),
            ": keepalive\n\n",
            _raw_sse(
                _event_payload(
                    "run.completed",
                    1,
                    output="x",
                    usage={},
                    runtime={},
                    completed=True,
                    partial=False,
                    interrupted=False,
                ),
                seq=1,
            ),
            ": stream closed\n\n",
        ]
    )

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("comments only")
            events = await _collect(client.events(admission.run_id))
            assert [event.kind for event in events] == [
                EventKind.MESSAGE_INTERIM,
                EventKind.RUN_COMPLETED,
            ]
            assert [event.seq for event in events] == [0, 1]
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_unknown_event_name_is_ignorable_and_stream_continues(
    gateway: Callable[..., FixtureGateway],
) -> None:
    stub = gateway(
        frames=[
            _event_frame("run.started", 0),
            _event_frame("message.delta", 1, delta="x"),
            _event_frame(
                "run.completed",
                2,
                output="x",
                usage={},
                runtime={},
                completed=True,
                partial=False,
                interrupted=False,
            ),
        ]
    )

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("unknown event")
            events = await _collect(client.events(admission.run_id))
            assert [event.kind for event in events] == [
                EventKind.UNKNOWN,
                EventKind.MESSAGE_INTERIM,
                EventKind.RUN_COMPLETED,
            ]
            assert events[0].raw_name == "run.started"
            assert events[-1].terminal is True
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_invalid_json_frame_raises_protocol_error(
    gateway: Callable[..., FixtureGateway],
) -> None:
    stub = gateway(raw_frames=["data: {not-json}\n\n"])

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("break the JSON")
            with pytest.raises(HermesProtocolError):
                await _collect(client.events(admission.run_id))
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_non_object_event_data_raises_protocol_error(
    gateway: Callable[..., FixtureGateway],
) -> None:
    stub = gateway(raw_frames=["data: [1, 2]\n\n"])

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("array payload")
            with pytest.raises(HermesProtocolError):
                await _collect(client.events(admission.run_id))
        finally:
            await client.aclose()

    asyncio.run(scenario())


def _without(data: Mapping[str, Any], key: str) -> dict[str, Any]:
    copied = dict(data)
    copied.pop(key)
    return copied


_ENVELOPE_DRIFT_CASES: list[tuple[str, dict[str, Any]]] = [
    ("missing-seq", _without(_event_payload("message.delta", 0, delta="x"), "seq")),
    ("seq-string", _event_payload("message.delta", "0", delta="x")),
    ("seq-bool", _event_payload("message.delta", True, delta="x")),
    ("missing-run-id", _without(_event_payload("message.delta", 0, delta="x"), "run_id")),
    ("run-id-int", _event_payload("message.delta", 0, delta="x", run_id=7)),
    ("timestamp-string", _event_payload("message.delta", 0, delta="x", timestamp="soon")),
    ("timestamp-bool", _event_payload("message.delta", 0, delta="x", timestamp=True)),
    ("timestamp-nan", _event_payload("message.delta", 0, delta="x", timestamp=float("nan"))),
    ("event-int", _event_payload(7, 0, delta="x")),
    ("missing-event", _without(_event_payload("message.delta", 0, delta="x"), "event")),
]


@pytest.mark.parametrize(
    "payload",
    [case[1] for case in _ENVELOPE_DRIFT_CASES],
    ids=[case[0] for case in _ENVELOPE_DRIFT_CASES],
)
def test_envelope_drift_raises_protocol_error(
    gateway: Callable[..., FixtureGateway], payload: dict[str, Any]
) -> None:
    stub = gateway(raw_frames=[_raw_sse(payload)])

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("drift the envelope")
            with pytest.raises(HermesProtocolError):
                await _collect(client.events(admission.run_id))
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_foreign_run_id_is_a_stream_contract_violation(
    gateway: Callable[..., FixtureGateway],
) -> None:
    foreign = _event_payload("message.delta", 0, delta="x")
    foreign["run_id"] = "run_other"
    stub = gateway(raw_frames=[_raw_sse(foreign, seq=0)])

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("foreign run id")
            with pytest.raises(HermesProtocolError) as raised:
                await _collect(client.events(admission.run_id))
            assert "contract violation" in str(raised.value)
            assert "run_other" in str(raised.value)
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_multiline_data_field_is_joined_before_decoding(
    gateway: Callable[..., FixtureGateway],
) -> None:
    first = 'data: {"event": "message.delta",'
    second = 'data: "run_id": "' + _RUN_ID + '", "timestamp": 1.0, "delta": "x", "seq": 0}'
    stub = gateway(
        raw_frames=[
            f"id: 0\n{first}\n{second}\n\n",
            _raw_sse(
                _event_payload(
                    "run.completed",
                    1,
                    output="x",
                    usage={},
                    runtime={},
                    completed=True,
                    partial=False,
                    interrupted=False,
                ),
                seq=1,
            ),
        ]
    )

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("multi-line data")
            events = await _collect(client.events(admission.run_id))
            assert [event.kind for event in events] == [
                EventKind.MESSAGE_INTERIM,
                EventKind.RUN_COMPLETED,
            ]
            assert events[0].text_delta == "x"
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_duplicate_sequence_frames_are_suppressed(
    gateway: Callable[..., FixtureGateway],
) -> None:
    stub = gateway(
        raw_frames=[
            _raw_sse(_event_payload("message.delta", 0, delta="x"), seq=0),
            _raw_sse(_event_payload("message.delta", 0, delta="x"), seq=0),
            _raw_sse(
                _event_payload(
                    "run.completed",
                    1,
                    output="x",
                    usage={},
                    runtime={},
                    completed=True,
                    partial=False,
                    interrupted=False,
                ),
                seq=1,
            ),
        ]
    )

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("duplicate seq")
            events = await _collect(client.events(admission.run_id))
            assert [event.seq for event in events] == [0, 1]
            assert sum(1 for event in events if event.terminal) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_duplicate_terminal_frame_yields_exactly_one_terminal(
    gateway: Callable[..., FixtureGateway],
) -> None:
    terminal = _event_payload(
        "run.completed",
        0,
        output="x",
        usage={},
        runtime={},
        completed=True,
        partial=False,
        interrupted=False,
    )
    stub = gateway(
        raw_frames=[
            _raw_sse(terminal, seq=0),
            _raw_sse(terminal, seq=0),
        ]
    )

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("duplicate terminal")
            events = await _collect(client.events(admission.run_id))
            assert [event.kind for event in events] == [EventKind.RUN_COMPLETED]
            assert sum(1 for event in events if event.terminal) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_swept_event_buffer_is_run_gone_but_status_survives(
    gateway: Callable[..., FixtureGateway],
) -> None:
    stub = gateway(
        "hermes_run_tool_turn.jsonl",
        events_gone=True,
        initial_status="completed",
        initial_output="Linux",
    )

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("already finished")
            with pytest.raises(HermesRunGoneError):
                await _collect(client.events(admission.run_id))

            status = await client.get_run(admission.run_id)
            assert status.status == "completed"
            assert status.output == "Linux"
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_status_fallback_synthesizes_failed_terminal(
    gateway: Callable[..., FixtureGateway],
) -> None:
    stub = gateway(
        None,
        frames=[],
        initial_status="failed",
        initial_output="The turn did not complete.",
    )

    async def scenario() -> None:
        client = _client(stub, max_reconnects=0)
        try:
            admission = await client.submit_run("fall back to a failed status")
            events = await _collect(client.events(admission.run_id))
            assert [event.kind for event in events] == [EventKind.RUN_FAILED]
            assert events[0].terminal is True
            assert events[0].completed is False
            assert events[0].interrupted is False
            assert events[0].error_message == "The turn did not complete."
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_status_fallback_synthesizes_cancelled_terminal(
    gateway: Callable[..., FixtureGateway],
) -> None:
    stub = gateway(None, frames=[], initial_status="cancelled", initial_output="")

    async def scenario() -> None:
        client = _client(stub, max_reconnects=0)
        try:
            admission = await client.submit_run("fall back to a cancelled status")
            events = await _collect(client.events(admission.run_id))
            assert [event.kind for event in events] == [EventKind.RUN_CANCELLED]
            assert events[0].interrupted is True
            assert events[0].turn_exit_reason == "interrupted_by_user"
            assert events[0].terminal is True
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_status_fallback_without_terminal_status_raises_stream_error(
    gateway: Callable[..., FixtureGateway],
) -> None:
    stub = gateway(None, frames=[], initial_status="running")

    async def scenario() -> None:
        client = _client(stub, max_reconnects=0)
        try:
            admission = await client.submit_run("stream that never ends")
            with pytest.raises(HermesStreamError):
                await _collect(client.events(admission.run_id))
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_bad_bearer_is_an_auth_error_and_creates_no_run(
    gateway: Callable[..., FixtureGateway],
) -> None:
    stub = gateway("hermes_run_tool_turn.jsonl")

    async def scenario() -> None:
        client = HermesClient(stub.base_url, "not-the-key", timeout_s=2.0)
        try:
            with pytest.raises(HermesAuthError):
                await client.submit_run("hello")
            assert stub.runs == {}
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_missing_input_is_a_typed_error_with_a_null_code(
    gateway: Callable[..., FixtureGateway],
) -> None:
    stub = gateway("hermes_run_tool_turn.jsonl")

    async def scenario() -> None:
        client = _client(stub)
        try:
            with pytest.raises(HermesError) as raised:
                await client.submit_run("")
            assert type(raised.value) is HermesError
            assert raised.value.code is None
            assert raised.value.message == "Missing 'input' field"
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_idempotency_conflict_is_a_conflict_error_and_is_not_retried(
    gateway: Callable[..., FixtureGateway],
) -> None:
    stub = gateway("hermes_run_tool_turn.jsonl")

    async def scenario() -> None:
        client = _client(stub)
        try:
            await client.submit_run("first payload", idempotency_key="idem-fixture-1")
            with pytest.raises(HermesConflictError) as raised:
                await client.submit_run("second payload", idempotency_key="idem-fixture-1")
            assert raised.value.code == "idempotency_key_conflict"

            posts = [
                request
                for request in stub.requests
                if request["method"] == "POST" and request["path"] == "/v1/runs"
            ]
            assert len(posts) == 2
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_plain_text_error_body_raises_protocol_error(
    gateway: Callable[..., FixtureGateway],
) -> None:
    stub = gateway(
        "hermes_run_tool_turn.jsonl",
        plain_text_error="Server got itself in trouble",
    )

    async def scenario() -> None:
        client = _client(stub)
        try:
            admission = await client.submit_run("plain text failure")
            with pytest.raises(HermesProtocolError) as raised:
                await _collect(client.events(admission.run_id))
            assert "non-JSON error response" in str(raised.value)
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_unknown_run_id_on_events_is_run_gone(
    gateway: Callable[..., FixtureGateway],
) -> None:
    stub = gateway("hermes_run_tool_turn.jsonl")

    async def scenario() -> None:
        client = _client(stub)
        try:
            with pytest.raises(HermesRunGoneError):
                await _collect(client.events("run_nope"))
        finally:
            await client.aclose()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Contract tests: documentation, decoder mapping and fixture schema must agree
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _EventSpec:
    kind: EventKind
    required: Mapping[str, type]
    optional: Mapping[str, type] = field(default_factory=dict)
    literals: Mapping[str, Any] = field(default_factory=dict)


_DOCUMENTED_SCHEMA: Mapping[str, _EventSpec] = {
    "tool.started": _EventSpec(
        EventKind.TOOL_STARTED,
        {"tool": str, "preview": str, "tool_call_id": str},
    ),
    "tool.completed": _EventSpec(
        EventKind.TOOL_COMPLETED,
        {
            "tool": str,
            "duration": int,
            "error": bool,
            "tool_call_id": str,
            "preview": str,
        },
    ),
    "approval.request": _EventSpec(
        EventKind.APPROVAL_REQUEST,
        {
            "kind": str,
            "prompt_id": str,
            "execution_generation": int,
            "command": str,
            "description": str,
            "choices": list,
        },
        literals={"kind": "approval"},
    ),
    "approval.settled": _EventSpec(
        EventKind.APPROVAL_SETTLED,
        {"prompt_id": str, "execution_generation": int},
    ),
    "clarify.request": _EventSpec(
        EventKind.CLARIFY_REQUEST,
        {
            "kind": str,
            "prompt_id": str,
            "execution_generation": int,
            "question": str,
            "choices": list,
            "multi_select": bool,
        },
        literals={"kind": "clarify"},
    ),
    "message.delta": _EventSpec(EventKind.MESSAGE_INTERIM, {"delta": str}),
    "run.completed": _EventSpec(
        EventKind.RUN_COMPLETED,
        {
            "output": str,
            "usage": dict,
            "runtime": dict,
            "completed": bool,
            "partial": bool,
            "interrupted": bool,
        },
    ),
    "run.cancelled": _EventSpec(
        EventKind.RUN_CANCELLED,
        {
            "completed": bool,
            "partial": bool,
            "interrupted": bool,
            "turn_exit_reason": str,
        },
    ),
    "run.failed": _EventSpec(
        EventKind.RUN_FAILED,
        {"error": str, "completed": bool, "partial": bool, "interrupted": bool},
    ),
    "replay.truncated": _EventSpec(
        EventKind.REPLAY_TRUNCATED,
        {"oldest_retained_seq": int},
        {"requested_seq": int},
    ),
}

_DOCUMENTED_KINDS = frozenset(spec.kind for spec in _DOCUMENTED_SCHEMA.values())

_ALIASES: Mapping[str, EventKind] = {
    "message.interim": EventKind.MESSAGE_INTERIM,
    "tool.start": EventKind.TOOL_STARTED,
    "tool.complete": EventKind.TOOL_COMPLETED,
    "approval.responded": EventKind.APPROVAL_SETTLED,
}

# Human-visible strings are synthetic.  Adding a fixture with new text must
# extend this allowlist, which is what keeps real user text out of the repo.
_SYNTHETIC_TEXT = frozenset(
    {
        "",
        "uname -s",
        "Linux",
        "\n\nLinux",
        "done",
        "\n\ndone",
        "Red",
        "\n\nRed",
        "rm -rf /tmp/hermes-talk-fixture-dir",
        "delete in temp path",
        "Which colour do you prefer, red or blue?",
        'Which colour do you prefer, red or blue?\nReply "skip" to skip this question.',
        "Red (Recommended)",
        "Blue",
        "once",
        "deny",
        "session",
        "always",
        "The turn did not complete.\nUse /retry to try again.",
    }
)

_VISIBLE_KEYS = frozenset(
    {"delta", "output", "error", "question", "command", "description", "preview"}
)
_HOME_PATH_RE = re.compile(r"(/home/[A-Za-z0-9_.-]+/|/Users/[A-Za-z0-9_.-]+/)")
_LAN_IP_RE = re.compile(
    r"\b(?:10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)\b"
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9_.%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _documented_event_names() -> set[str]:
    """Event names the contract document says the run stream can emit."""
    text = _CONTRACT_DOC.read_text(encoding="utf-8")
    section = text.split("Event names observed on this surface:", 1)[1]
    section = section.split("The implementation's wider vocabulary", 1)[0]
    names = set(re.findall(r"^\|\s*`([a-z][a-z0-9.]*)`\s*\|", section, re.M))
    assert "`replay.truncated`" in text
    return names | {"replay.truncated"}


def _fixture_paths() -> list[Path]:
    return sorted(_FIXTURES_DIR.glob("*.jsonl"))


def _event_entries(path: Path) -> list[tuple[int, dict[str, Any]]]:
    entries: list[tuple[int, dict[str, Any]]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        entry = json.loads(line)
        assert isinstance(entry, dict), f"{path.name}:{line_number} is not an object"
        if "comment" in entry:
            assert isinstance(entry["comment"], str)
            assert set(entry) == {"comment"}
            continue
        assert set(entry) == {"id", "data"}, f"{path.name}:{line_number} has unexpected keys"
        entries.append((line_number, entry))
    return entries


def _type_ok(value: Any, expected: type) -> bool:
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is float:
        return (
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        )
    if expected is str:
        return isinstance(value, str)
    if expected is list:
        return isinstance(value, list)
    if expected is dict:
        return isinstance(value, dict)
    return isinstance(value, expected)


def _validate_envelope(where: str, data: Mapping[str, Any]) -> None:
    missing = _ENVELOPE_KEYS - set(data)
    assert not missing, f"{where}: missing envelope keys {sorted(missing)}"
    assert isinstance(data["event"], str), f"{where}: event must be a string"
    seq = data["seq"]
    assert isinstance(seq, int) and not isinstance(seq, bool), f"{where}: seq must be an int"
    assert seq >= 0, f"{where}: seq must be non-negative"
    assert isinstance(data["run_id"], str) and data["run_id"], f"{where}: bad run_id"
    timestamp = data["timestamp"]
    assert _type_ok(timestamp, float), f"{where}: timestamp must be finite"


def _validate_payload(where: str, name: str, spec: _EventSpec, payload: Mapping[str, Any]) -> None:
    allowed = set(spec.required) | set(spec.optional)
    unexpected = set(payload) - allowed
    assert not unexpected, f"{where}: {name} has undocumented payload keys {sorted(unexpected)}"
    for key, expected in spec.required.items():
        assert key in payload, f"{where}: {name} is missing required field {key!r}"
        assert _type_ok(payload[key], expected), (
            f"{where}: {name}.{key} must be {expected.__name__}, got {type(payload[key]).__name__}"
        )
    for key, expected in spec.optional.items():
        if key in payload:
            assert _type_ok(payload[key], expected), (
                f"{where}: {name}.{key} must be {expected.__name__}"
            )
    for key, literal in spec.literals.items():
        assert payload.get(key) == literal, f"{where}: {name}.{key} must be {literal!r}"


def test_documented_event_names_match_the_contract_document() -> None:
    assert _documented_event_names() == set(_DOCUMENTED_SCHEMA)
    assert "run.completed` and `run.cancelled` are the terminal frames" in _CONTRACT_DOC.read_text(
        encoding="utf-8"
    )


def test_decoder_vocabulary_matches_the_documented_schema() -> None:
    assert {kind.value for kind in EventKind} == {kind.value for kind in _DOCUMENTED_KINDS} | {
        "unknown"
    }
    assert {
        EventKind.RUN_COMPLETED,
        EventKind.RUN_CANCELLED,
        EventKind.RUN_FAILED,
    } == TERMINAL_KINDS


@pytest.mark.parametrize("name", sorted(_DOCUMENTED_SCHEMA))
def test_documented_event_decodes_to_its_documented_kind(name: str) -> None:
    spec = _DOCUMENTED_SCHEMA[name]
    frame = SseFrame(
        kind="data",
        data=json.dumps(
            {"event": name, "seq": 0, "run_id": _RUN_ID, "timestamp": 1.0},
        ),
        event_id=0,
        retry_ms=None,
    )
    event = decode_event(frame)
    assert event.kind is spec.kind
    assert event.raw_name == name


def test_implementation_aliases_decode_to_documented_kinds() -> None:
    for name, expected in _ALIASES.items():
        frame = SseFrame(
            kind="data",
            data=json.dumps({"event": name, "seq": 0, "run_id": _RUN_ID, "timestamp": 1.0}),
            event_id=0,
            retry_ms=None,
        )
        assert decode_event(frame).kind is expected, name

    frame = SseFrame(
        kind="data",
        data=json.dumps({"event": "run.started", "seq": 0, "run_id": _RUN_ID, "timestamp": 1.0}),
        event_id=0,
        retry_ms=None,
    )
    unknown = decode_event(frame)
    assert unknown.kind is EventKind.UNKNOWN
    assert unknown.raw_name == "run.started"


def test_every_documented_event_type_has_a_fixture() -> None:
    covered: set[str] = set()
    for path in _fixture_paths():
        for _line_number, entry in _event_entries(path):
            covered.add(entry["data"]["event"])
    missing = set(_DOCUMENTED_SCHEMA) - covered
    assert not missing, f"documented events without a fixture: {sorted(missing)}"


def test_fixtures_match_the_documented_envelope_and_payload_schema() -> None:
    for path in _fixture_paths():
        run_ids: set[str] = set()
        seqs: list[int] = []
        terminal_positions: list[int] = []
        entries = _event_entries(path)
        for index, (line_number, entry) in enumerate(entries):
            where = f"{path.name}:{line_number}"
            data = entry["data"]
            _validate_envelope(where, data)
            name = data["event"]
            assert name in _DOCUMENTED_SCHEMA, f"{where}: undocumented event {name!r}"
            payload = {key: value for key, value in data.items() if key not in _ENVELOPE_KEYS}
            _validate_payload(where, name, _DOCUMENTED_SCHEMA[name], payload)
            assert entry["id"] == data["seq"], f"{where}: SSE id must repeat seq"
            run_ids.add(data["run_id"])
            seqs.append(data["seq"])
            if data["event"] in _TERMINAL_NAMES:
                terminal_positions.append(index)

        assert len(run_ids) == 1, f"{path.name}: frames disagree on run_id"
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), (
            f"{path.name}: seqs are not consecutive"
        )
        assert len(terminal_positions) == 1, f"{path.name}: needs exactly one terminal frame"
        assert terminal_positions[0] == len(entries) - 1, (
            f"{path.name}: the terminal frame must be the last event"
        )
        terminal = entries[-1][1]["data"]
        assert _DOCUMENTED_SCHEMA[terminal["event"]].kind in TERMINAL_KINDS


def test_fixtures_are_small_and_free_of_personal_data() -> None:
    for path in _fixture_paths():
        assert path.stat().st_size <= _MAX_FIXTURE_BYTES, f"{path.name} exceeds 2 MiB"
        text = path.read_text(encoding="utf-8")
        assert not _HOME_PATH_RE.search(text), f"{path.name} contains an absolute home path"
        assert not _LAN_IP_RE.search(text), f"{path.name} contains a private LAN address"
        assert not _EMAIL_RE.search(text), f"{path.name} contains an email address"


def test_fixture_visible_text_is_synthetic() -> None:
    for path in _fixture_paths():
        for _line_number, entry in _event_entries(path):
            data = entry["data"]
            values: list[str] = [
                data[key] for key in _VISIBLE_KEYS if isinstance(data.get(key), str)
            ]
            choices = data.get("choices")
            if isinstance(choices, list):
                values.extend(choices)
            for value in values:
                assert isinstance(value, str), f"{path.name}: visible text must be a string"
                assert value in _SYNTHETIC_TEXT, (
                    f"{path.name}: {value!r} is not in the synthetic-text allowlist"
                )
