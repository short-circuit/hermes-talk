"""Offline integration tests for the relay's control paths.

The suite composes the production modules that own each control surface against
an in-process loopback gateway that replays the synthetic SSE fixtures in
``relay/tests/fixtures``.  Nothing leaves ``127.0.0.1``: no cloud STT, TTS or
LLM, no Hermes gateway, no real microphone.

Covered paths, one test each:

* approval accepted — the spoken gate's wire body is posted once and the
  gateway's ``approval.settled`` frame echoes the answered ids;
* approval denied — a spoken negation posts ``choice=deny``;
* approval timeout — expiry posts ``choice=deny`` without any utterance, and
  the documented timeout ordering (``tool.completed`` before
  ``approval.settled``) is tolerated;
* barge-in stop — a loud frame flushes playback and posts
  ``POST /v1/runs/{id}/stop``, and the stream ends ``run.cancelled``;
* clarify — the question is decoded and spoken, a wrong key name is rejected
  with ``invalid_params``, and the answered prompt is consumed exactly once;
* run failure during narration — narration stops at ``run.failed``, the
  failure phrase is spoken instead of the raw error, and the throttle rebases.

Every failure is re-raised with the observed event trace attached by
``trace_guard`` so a broken control path reports the sequence that led to it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from relay.brain import (
    ApprovalGate,
    ApprovalRequest,
    ControlId,
    Decision,
    Provenance,
    ToolNarrator,
    Utterance,
    delegation_failure,
    redact_spoken,
)
from relay.hermes.client import HermesClient
from relay.hermes.events import EventKind, RunEvent
from relay.limits import BargeInGate
from relay.speech.audio import PcmChunk
from relay.transport import (
    EndOfTurnDetector,
    EnergyVad,
    HttpRunStopper,
    TurnController,
    TurnState,
    VadConfig,
)

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_API_KEY = "stub-control-key-0123456789"
_SESSION_KEY = "voice-relay-controls"
_RUN_ID = "run_controls00000000000000000000000001"
_LOOPBACK_HOST = "127.0.0.1"
_TERMINAL_NAMES = frozenset({"run.completed", "run.cancelled", "run.failed"})
_STATUS_BY_EVENT = {
    "run.completed": "completed",
    "run.cancelled": "cancelled",
    "run.failed": "failed",
}
_APPROVAL_CHOICES = frozenset({"once", "deny", "session", "always"})
_APPROVAL_KEYS = frozenset({"request_id", "execution_generation", "choice"})
_CLARIFY_KEYS = frozenset({"request_id", "execution_generation", "answer"})
_PROMPT_EVENTS = frozenset({"approval.request", "clarify.request"})
_STREAM_WAIT_S = 5.0
_MAX_TRACE_ENTRIES = 200


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


# ---------------------------------------------------------------------------
# Event trace: every failure carries the observed sequence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceEntry:
    """One observed run event or control-plane request."""

    label: str
    detail: str


class EventTrace:
    """Ordered, bounded record of what the harness observed.

    It records only event identity and ids, never raw text, so a trace is safe
    to paste into a failure report.
    """

    def __init__(self, max_entries: int = _MAX_TRACE_ENTRIES) -> None:
        self._entries: list[TraceEntry] = []
        self._max_entries = max_entries
        self._notes: list[str] = []

    def event(self, event: RunEvent) -> None:
        bits = [f"kind={event.kind.value}", f"seq={event.seq}"]
        if event.tool is not None:
            bits.append(f"tool={event.tool}")
        if event.prompt_id is not None:
            bits.append(f"prompt_id={event.prompt_id}")
        if event.execution_generation is not None:
            bits.append(f"generation={event.execution_generation}")
        if event.error_message is not None:
            bits.append("error=present")
        self._add("run.event", " ".join(bits))

    def wire(
        self,
        method: str,
        path: str,
        status: int,
        payload: Mapping[str, Any] | None,
    ) -> None:
        keys = "-" if payload is None else ",".join(sorted(payload))
        self._add(f"wire {method} {path}", f"status={status} payload_keys=[{keys}]")

    def note(self, text: str) -> None:
        self._notes.append(text)

    def _add(self, label: str, detail: str) -> None:
        if len(self._entries) < self._max_entries:
            self._entries.append(TraceEntry(label=label, detail=detail))

    def render(self) -> str:
        lines = ["event trace:"]
        lines.extend(
            f"  {index:02d}. {entry.label}: {entry.detail}"
            for index, entry in enumerate(self._entries, 1)
        )
        if self._notes:
            lines.append("notes:")
            lines.extend(f"  - {note}" for note in self._notes)
        if not self._entries and not self._notes:
            lines.append("  (empty)")
        return "\n".join(lines)


@contextmanager
def trace_guard(trace: EventTrace) -> Iterator[None]:
    """Re-raise any failure with the observed event trace appended."""
    try:
        yield
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        raise AssertionError(f"{type(exc).__name__}: {exc}\n\n{trace.render()}") from exc


# ---------------------------------------------------------------------------
# Loopback gateway fixture
# ---------------------------------------------------------------------------


class ControlGateway:
    """In-process Hermes API double with the full control surface.

    It replays one recorded SSE fixture and, unlike the run-path stub, owns the
    approval, clarify and stop endpoints.  A prompt frame pauses the stream
    until the relay posts an answer, so the settled frame can only arrive after
    the answer and can echo exactly the ids the relay sent.
    """

    def __init__(
        self,
        fixture: str | None = None,
        *,
        api_key: str = _API_KEY,
        run_id: str = _RUN_ID,
        pause_at_prompt: bool = True,
        hold_open: bool = False,
    ) -> None:
        self.api_key = api_key
        self.run_id = run_id
        self.frames = _load_fixture(fixture) if fixture is not None else []
        self.pause_at_prompt = pause_at_prompt
        self.hold_open = hold_open
        self.controls: dict[str, dict[str, Any]] = {}
        self.answers: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.runs: dict[str, dict[str, Any]] = {}
        self._register_controls()
        self.app = self._build_app()
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.base_url = ""

    def _register_controls(self) -> None:
        for frame in self.frames:
            data = frame.get("data")
            if not isinstance(data, Mapping) or data.get("event") not in _PROMPT_EVENTS:
                continue
            self.controls[str(data["prompt_id"])] = {
                "kind": "approval" if data["event"] == "approval.request" else "clarify",
                "prompt_id": data["prompt_id"],
                "execution_generation": data["execution_generation"],
            }

    def start(self) -> None:
        """Start the stub server and wait until it is accepting requests."""
        config = uvicorn.Config(
            self.app,
            host=_LOOPBACK_HOST,
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
                msg = "stub control server did not start"
                raise RuntimeError(msg)
            time.sleep(0.01)

        server = self.server
        if server is None or not server.servers:
            msg = "stub control server has no listening socket"
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
                "json": body,
            }
        )

    def _new_run(self, session_id: str | None) -> dict[str, Any]:
        return {
            "status": "running",
            "session_id": session_id or self.run_id,
            "admission_id": "adm_stub00000000000000000000000002",
            "execution_generation": 1,
            "output": "",
            "stop_event": asyncio.Event(),
            "answer_event": asyncio.Event(),
            "answer": None,
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
            if not isinstance(body, dict) or not body.get("input"):
                return _error("Missing 'input' field", None, 400)
            self.runs[self.run_id] = self._new_run(body.get("session_id"))
            return JSONResponse(
                {"run_id": self.run_id, "status": "started", "replayed": False},
                status_code=202,
            )

        @app.get("/v1/runs/{run_id}/events")
        async def run_events(run_id: str, request: Request) -> Any:
            if not self._authorized(request):
                return _auth_error()
            self._record(request)
            run = self.runs.get(run_id)
            if run is None:
                return _error(f"Run not found: {run_id}", "run_not_found", 404)
            return StreamingResponse(
                self._stream(run, run_id),
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
                    "pending_controls": list(self.controls.values()),
                    "run_id": run_id,
                    "status": run["status"],
                    "session_id": run["session_id"],
                    "admission_id": run["admission_id"],
                    "execution_generation": run["execution_generation"],
                    "output": run["output"],
                    "usage": {},
                }
            )

        @app.post("/v1/runs/{run_id}/approval")
        async def answer_approval(run_id: str, request: Request) -> JSONResponse:
            if not self._authorized(request):
                return _auth_error()
            body = await self._read_body(request)
            if body is None:
                return _error("Invalid JSON", None, 400)
            self._record(request, body)
            if not isinstance(body, dict):
                return _error("Invalid JSON", None, 400)
            if set(body) != _APPROVAL_KEYS:
                return _error("invalid_params", "invalid_params", 400)
            prompt_id = body.get("request_id")
            generation = body.get("execution_generation")
            choice = body.get("choice")
            if (
                not isinstance(prompt_id, str)
                or not isinstance(generation, int)
                or isinstance(generation, bool)
                or choice not in _APPROVAL_CHOICES
            ):
                return _error("invalid_params", "invalid_params", 400)
            control = self.controls.get(prompt_id)
            if control is None or control["kind"] != "approval":
                return _error("approval_not_pending", "approval_not_pending", 409)
            if control["execution_generation"] != generation:
                return _error("stale_generation", "stale_generation", 409)
            del self.controls[prompt_id]
            self.answers.append(
                {
                    "kind": "approval",
                    "request_id": prompt_id,
                    "execution_generation": generation,
                    "choice": choice,
                }
            )
            self._release(run_id, prompt_id, generation)
            return JSONResponse(
                {"run_id": run_id, "status": "resolved", "prompt_id": prompt_id},
            )

        @app.post("/v1/runs/{run_id}/clarify")
        async def answer_clarify(run_id: str, request: Request) -> JSONResponse:
            if not self._authorized(request):
                return _auth_error()
            body = await self._read_body(request)
            if body is None:
                return _error("Invalid JSON", None, 400)
            self._record(request, body)
            if not isinstance(body, dict):
                return _error("Invalid JSON", None, 400)
            if set(body) != _CLARIFY_KEYS:
                return _error("invalid_params", "invalid_params", 400)
            prompt_id = body.get("request_id")
            generation = body.get("execution_generation")
            answer = body.get("answer")
            if (
                not isinstance(prompt_id, str)
                or not isinstance(generation, int)
                or isinstance(generation, bool)
                or not isinstance(answer, str)
                or not answer.strip()
            ):
                return _error("invalid_params", "invalid_params", 400)
            control = self.controls.get(prompt_id)
            if control is None or control["kind"] != "clarify":
                return _error("approval_not_pending", "approval_not_pending", 409)
            if control["execution_generation"] != generation:
                return _error("stale_generation", "stale_generation", 409)
            del self.controls[prompt_id]
            self.answers.append(
                {
                    "kind": "clarify",
                    "request_id": prompt_id,
                    "execution_generation": generation,
                    "answer": answer,
                }
            )
            self._release(run_id, prompt_id, generation)
            return JSONResponse(
                {"run_id": run_id, "status": "resolved", "prompt_id": prompt_id},
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

    @staticmethod
    async def _read_body(request: Request) -> Any:
        try:
            return await request.json()
        except ValueError:
            return None

    def _release(self, run_id: str, prompt_id: str, generation: int) -> None:
        run = self.runs.get(run_id)
        if run is None:
            return
        run["answer"] = {
            "request_id": prompt_id,
            "execution_generation": generation,
        }
        run["answer_event"].set()

    async def _stream(self, run: dict[str, Any], run_id: str) -> AsyncIterator[str]:
        yield ": open\n\n"
        emitted = -1
        for frame in self.frames:
            if "comment" in frame:
                yield f": {frame['comment']}\n\n"
                continue
            seq = frame.get("id")
            if not isinstance(seq, int) or seq < 0:
                continue
            data = frame["data"]
            name = data.get("event")
            if self.hold_open and name in _TERMINAL_NAMES:
                continue
            if self.pause_at_prompt and name in _PROMPT_EVENTS:
                yield _render(frame, run_id)
                emitted = max(emitted, seq)
                await run["answer_event"].wait()
                continue
            if self.pause_at_prompt and name == "approval.settled":
                answer = run["answer"]
                if answer is not None:
                    rewritten = dict(data)
                    rewritten["prompt_id"] = answer["request_id"]
                    rewritten["execution_generation"] = answer["execution_generation"]
                    frame = {"id": seq, "data": rewritten}
            yield _render(frame, run_id)
            emitted = max(emitted, seq)
            if name in _TERMINAL_NAMES:
                self._settle(run, frame)
                yield ": stream closed\n\n"
                return

        if not self.hold_open:
            yield ": stream closed\n\n"
            return

        await run["stop_event"].wait()
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
    def _settle(run: dict[str, Any], frame: Mapping[str, Any]) -> None:
        event = frame["data"].get("event")
        status = _STATUS_BY_EVENT.get(event)
        if status is not None:
            run["status"] = status
        run["output"] = str(frame["data"].get("output", ""))


class ControlPoster:
    """The relay-side caller that posts a resolved control to the gateway."""

    def __init__(self, base_url: str, api_key: str, trace: EventTrace) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=5.0)
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._trace = trace

    async def post(self, path: str, payload: Mapping[str, Any]) -> tuple[httpx.Response, Any]:
        response = await self._client.post(path, json=dict(payload), headers=self._headers)
        self._trace.wire("POST", path, response.status_code, payload)
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        return response, body

    async def aclose(self) -> None:
        await self._client.aclose()


@pytest.fixture
def gateway() -> Iterator[Callable[..., ControlGateway]]:
    servers: list[ControlGateway] = []

    def _make(fixture: str | None = None, **knobs: Any) -> ControlGateway:
        server = ControlGateway(fixture, **knobs)
        server.start()
        servers.append(server)
        return server

    yield _make
    for server in servers:
        server.stop()


def _client(stub: ControlGateway) -> HermesClient:
    return HermesClient(
        stub.base_url,
        _API_KEY,
        session_key=_SESSION_KEY,
        timeout_s=2.0,
        connect_timeout_s=1.0,
        stop_timeout_s=2.0,
        stream_timeout_s=5.0,
        reconnect_backoff_s=0.01,
        max_reconnect_backoff_s=0.05,
    )


class FakeClock:
    """Deterministic monotonic clock for the turn and narration paths."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class FakePlayback:
    """Playback sink that counts flushes."""

    def __init__(self) -> None:
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1


def _loud_chunk(duration_ms: int = 20) -> PcmChunk:
    samples = np.full(int(16000 * duration_ms / 1000), 16000, dtype=np.int16)
    return PcmChunk(data=samples.tobytes(), sample_rate=16000)


def _approval_request(
    prompt: RunEvent,
    started: RunEvent,
    run_id: str,
    *,
    timeout_s: float = 20.0,
) -> ApprovalRequest:
    command = str(prompt.payload["command"])
    description = str(prompt.payload["description"])
    return ApprovalRequest(
        prompt_id=str(prompt.prompt_id),
        execution_generation=int(prompt.execution_generation or 0),
        tool=started.tool or "terminal",
        action_summary=description,
        required_phrase="yes run it",
        timeout_s=timeout_s,
        command=command,
        description=description,
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Approval: accept
# ---------------------------------------------------------------------------


def test_approval_accept_posts_the_exact_wire_payload_and_settles(
    gateway: Callable[..., ControlGateway],
) -> None:
    stub = gateway("hermes_run_approval.jsonl")
    trace = EventTrace()

    async def scenario() -> None:
        client = _client(stub)
        poster = ControlPoster(stub.base_url, _API_KEY, trace)
        try:
            admission = await client.submit_run("delete the fixture directory")
            events: list[RunEvent] = []
            prompt_seen = asyncio.Event()

            async def collect() -> None:
                async for event in client.events(admission.run_id):
                    trace.event(event)
                    events.append(event)
                    if event.kind is EventKind.APPROVAL_REQUEST:
                        prompt_seen.set()

            collector = asyncio.create_task(collect())
            await asyncio.wait_for(prompt_seen.wait(), timeout=_STREAM_WAIT_S)
            prompt = next(e for e in events if e.kind is EventKind.APPROVAL_REQUEST)
            started = next(e for e in events if e.kind is EventKind.TOOL_STARTED)
            assert prompt.payload["kind"] == "approval"
            assert prompt.execution_generation == 1
            assert prompt.payload["command"] == "rm -rf /tmp/hermes-talk-fixture-dir"
            assert started.tool == "terminal"

            decisions: list[Decision] = []
            gate = ApprovalGate(auto_approve_read_only=False, on_decide=decisions.append)
            request = _approval_request(prompt, started, admission.run_id)
            assert gate.request(request, now=100.0) is request

            readout = gate.readout()
            assert readout.command == request.command
            assert readout.text.count(request.command or "") == 1
            assert (
                readout.command_sha256
                == hashlib.sha256((request.command or "").encode("utf-8")).hexdigest()
            )
            assert readout.deadline_at == 120.0

            decision = gate.answer(
                Utterance(text="yes, run it", provenance=Provenance.SPOKEN, confidence=0.95),
                control=ControlId(request.prompt_id, request.execution_generation),
                now=101.0,
            )
            assert isinstance(decision, Decision)
            assert decision.approved is True
            assert decision.reason == "spoken_phrase"
            assert decision.wire_payload() == {
                "request_id": request.prompt_id,
                "execution_generation": 1,
                "choice": "once",
            }
            assert decisions == [decision]
            assert gate.post_error is None

            response, body = await poster.post(
                f"/v1/runs/{admission.run_id}/approval",
                decision.wire_payload(),
            )
            assert response.status_code == 200
            assert body == {
                "run_id": admission.run_id,
                "status": "resolved",
                "prompt_id": request.prompt_id,
            }
            assert stub.answers == [
                {
                    "kind": "approval",
                    "request_id": request.prompt_id,
                    "execution_generation": 1,
                    "choice": "once",
                }
            ]

            duplicate = gate.answer(
                Utterance(text="yes run it", provenance=Provenance.SPOKEN),
                control=ControlId(request.prompt_id, request.execution_generation),
                now=102.0,
            )
            assert duplicate is None
            assert len(stub.answers) == 1

            await asyncio.wait_for(collector, timeout=_STREAM_WAIT_S)
            settled = [e for e in events if e.kind is EventKind.APPROVAL_SETTLED]
            assert len(settled) == 1
            assert settled[0].prompt_id == request.prompt_id
            assert settled[0].execution_generation == 1
            assert events[-1].kind is EventKind.RUN_COMPLETED
            trace.note("approval accepted, posted once, settled with the answered ids")
        finally:
            await poster.aclose()
            await client.aclose()

    with trace_guard(trace):
        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Approval: deny
# ---------------------------------------------------------------------------


def test_approval_deny_posts_choice_deny(
    gateway: Callable[..., ControlGateway],
) -> None:
    stub = gateway("hermes_run_approval.jsonl")
    trace = EventTrace()

    async def scenario() -> None:
        client = _client(stub)
        poster = ControlPoster(stub.base_url, _API_KEY, trace)
        try:
            admission = await client.submit_run("delete the fixture directory")
            events: list[RunEvent] = []
            prompt_seen = asyncio.Event()

            async def collect() -> None:
                async for event in client.events(admission.run_id):
                    trace.event(event)
                    events.append(event)
                    if event.kind is EventKind.APPROVAL_REQUEST:
                        prompt_seen.set()

            collector = asyncio.create_task(collect())
            await asyncio.wait_for(prompt_seen.wait(), timeout=_STREAM_WAIT_S)
            prompt = next(e for e in events if e.kind is EventKind.APPROVAL_REQUEST)
            started = next(e for e in events if e.kind is EventKind.TOOL_STARTED)

            decisions: list[Decision] = []
            gate = ApprovalGate(auto_approve_read_only=False, on_decide=decisions.append)
            request = _approval_request(prompt, started, admission.run_id)
            gate.request(request, now=100.0)

            decision = gate.answer(
                Utterance(
                    text="no, do not run it",
                    provenance=Provenance.SPOKEN,
                    confidence=0.95,
                ),
                control=ControlId(request.prompt_id, request.execution_generation),
                now=101.0,
            )
            assert isinstance(decision, Decision)
            assert decision.approved is False
            assert decision.reason == "spoken_negation"
            assert decision.wire_payload() == {
                "request_id": request.prompt_id,
                "execution_generation": 1,
                "choice": "deny",
            }
            assert decisions == [decision]
            assert gate.pending is None
            assert gate.post_error is None

            response, body = await poster.post(
                f"/v1/runs/{admission.run_id}/approval",
                decision.wire_payload(),
            )
            assert response.status_code == 200
            assert body == {
                "run_id": admission.run_id,
                "status": "resolved",
                "prompt_id": request.prompt_id,
            }
            assert stub.answers == [
                {
                    "kind": "approval",
                    "request_id": request.prompt_id,
                    "execution_generation": 1,
                    "choice": "deny",
                }
            ]

            await asyncio.wait_for(collector, timeout=_STREAM_WAIT_S)
            assert events[-1].kind is EventKind.RUN_COMPLETED
            trace.note("approval denied with choice=deny")
        finally:
            await poster.aclose()
            await client.aclose()

    with trace_guard(trace):
        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Approval: timeout
# ---------------------------------------------------------------------------


def test_approval_timeout_posts_deny_without_an_utterance(
    gateway: Callable[..., ControlGateway],
) -> None:
    stub = gateway("hermes_run_approval_timeout.jsonl")
    trace = EventTrace()

    async def scenario() -> None:
        client = _client(stub)
        poster = ControlPoster(stub.base_url, _API_KEY, trace)
        try:
            admission = await client.submit_run("delete the fixture directory")
            events: list[RunEvent] = []
            prompt_seen = asyncio.Event()

            async def collect() -> None:
                async for event in client.events(admission.run_id):
                    trace.event(event)
                    events.append(event)
                    if event.kind is EventKind.APPROVAL_REQUEST:
                        prompt_seen.set()

            collector = asyncio.create_task(collect())
            await asyncio.wait_for(prompt_seen.wait(), timeout=_STREAM_WAIT_S)
            prompt = next(e for e in events if e.kind is EventKind.APPROVAL_REQUEST)
            started = next(e for e in events if e.kind is EventKind.TOOL_STARTED)

            decisions: list[Decision] = []
            gate = ApprovalGate(auto_approve_read_only=False, on_decide=decisions.append)
            request = _approval_request(prompt, started, admission.run_id, timeout_s=5.0)
            gate.request(request, now=100.0)
            assert gate.readout().deadline_at == 105.0

            assert gate.expire(now=104.9) is None
            assert gate.decision() is None
            decision = gate.expire(now=105.1)
            assert isinstance(decision, Decision)
            assert decision.approved is False
            assert decision.reason == "timeout"
            assert decision.wire_payload() == {
                "request_id": request.prompt_id,
                "execution_generation": 1,
                "choice": "deny",
            }
            assert decisions == [decision]
            assert gate.post_error is None

            response, body = await poster.post(
                f"/v1/runs/{admission.run_id}/approval",
                decision.wire_payload(),
            )
            assert response.status_code == 200
            assert body == {
                "run_id": admission.run_id,
                "status": "resolved",
                "prompt_id": request.prompt_id,
            }
            assert stub.answers == [
                {
                    "kind": "approval",
                    "request_id": request.prompt_id,
                    "execution_generation": 1,
                    "choice": "deny",
                }
            ]

            late = gate.answer(
                Utterance(text="yes run it", provenance=Provenance.SPOKEN),
                control=ControlId(request.prompt_id, request.execution_generation),
                now=105.2,
            )
            assert late is None
            assert len(stub.answers) == 1

            await asyncio.wait_for(collector, timeout=_STREAM_WAIT_S)
            tool_completed = [e for e in events if e.kind is EventKind.TOOL_COMPLETED]
            settled = [e for e in events if e.kind is EventKind.APPROVAL_SETTLED]
            assert len(tool_completed) == 1
            assert tool_completed[0].payload["error"] is True
            assert len(settled) == 1
            assert settled[0].prompt_id == request.prompt_id
            assert settled[0].execution_generation == 1
            assert events.index(tool_completed[0]) < events.index(settled[0])
            assert events[-1].kind is EventKind.RUN_COMPLETED

            status = await client.get_run(admission.run_id)
            assert status.status == "completed"
            assert status.pending_controls == ()
            trace.note("approval timed out, deny posted without an utterance")
        finally:
            await poster.aclose()
            await client.aclose()

    with trace_guard(trace):
        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Barge-in stop
# ---------------------------------------------------------------------------


def test_barge_in_stop_flushes_playback_and_ends_the_stream_cancelled(
    gateway: Callable[..., ControlGateway],
) -> None:
    stub = gateway(
        "hermes_run_approval.jsonl",
        pause_at_prompt=False,
        hold_open=True,
    )
    trace = EventTrace()

    async def scenario() -> None:
        client = _client(stub)
        clock = FakeClock()
        playback = FakePlayback()
        controller = TurnController(
            vad=EnergyVad(VadConfig(), clock=clock),
            detector=EndOfTurnDetector(),
            gate=BargeInGate(min_speech_ms=300, cooldown_ms=1000, clock=clock),
            playback=playback,
            stopper=HttpRunStopper(stub.base_url, _API_KEY, timeout_s=2.0),
            clock=clock,
        )
        try:
            admission = await client.submit_run("give me a long answer")
            events: list[RunEvent] = []
            first_event = asyncio.Event()

            async def collect() -> None:
                async for event in client.events(admission.run_id):
                    trace.event(event)
                    events.append(event)
                    first_event.set()

            collector = asyncio.create_task(collect())
            await asyncio.wait_for(first_event.wait(), timeout=_STREAM_WAIT_S)

            controller.begin_user_turn()
            controller.commit_utterance("what time is it")
            controller.begin_reply(run_id=admission.run_id)
            controller.note_spoken("The answer is forty two")

            outcomes = []
            for _ in range(15):
                clock.advance(0.02)
                outcomes.append(await controller.feed_mic(_loud_chunk()))

            confirmed = [
                outcome
                for outcome in outcomes
                if outcome.barge_in is not None and outcome.barge_in.stop_attempted
            ]
            assert confirmed, "no barge-in stop was confirmed"
            result = confirmed[0].barge_in
            assert result is not None
            assert result.flushed is True
            assert result.stop_attempted is True
            assert result.stop_accepted is True
            assert result.run_stop is not None
            assert result.run_stop.ok is True
            assert result.run_stop.run_id == admission.run_id
            assert result.run_stop.status == "stopping"
            assert playback.flushes == 1

            stop_requests = [
                request for request in stub.requests if request["path"].endswith("/stop")
            ]
            assert len(stop_requests) == 1
            assert stop_requests[0]["path"] == f"/v1/runs/{admission.run_id}/stop"

            await asyncio.wait_for(collector, timeout=_STREAM_WAIT_S)
            assert events[-1].kind is EventKind.RUN_CANCELLED
            assert events[-1].interrupted is True
            assert not any(event.kind is EventKind.RUN_COMPLETED for event in events)

            ledger_kinds = [event.kind for event in controller.ledger.events()]
            assert "barge_in.flushed" in ledger_kinds
            assert "turn.interrupted" in ledger_kinds
            assert controller.snapshot()["turn_state"] == TurnState.INTERRUPTED.value
            assert controller.snapshot()["interruptions"] == 1

            marker = controller.take_marker()
            assert marker is not None
            assert "interrupted" in marker
            assert "forty two" in marker
            assert controller.take_marker() is None

            status = await client.get_run(admission.run_id)
            assert status.status == "cancelled"
            trace.note("barge-in flushed playback, posted stop, run cancelled")
        finally:
            await client.aclose()

    with trace_guard(trace):
        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Clarify
# ---------------------------------------------------------------------------


def test_clarify_question_is_spoken_and_the_answer_is_posted_once(
    gateway: Callable[..., ControlGateway],
) -> None:
    stub = gateway("hermes_run_clarify.jsonl")
    trace = EventTrace()

    async def scenario() -> None:
        client = _client(stub)
        poster = ControlPoster(stub.base_url, _API_KEY, trace)
        try:
            admission = await client.submit_run("ask me which colour to use")
            events: list[RunEvent] = []
            prompt_seen = asyncio.Event()

            async def collect() -> None:
                async for event in client.events(admission.run_id):
                    trace.event(event)
                    events.append(event)
                    if event.kind is EventKind.CLARIFY_REQUEST:
                        prompt_seen.set()

            collector = asyncio.create_task(collect())
            await asyncio.wait_for(prompt_seen.wait(), timeout=_STREAM_WAIT_S)
            prompt = next(e for e in events if e.kind is EventKind.CLARIFY_REQUEST)
            question = str(prompt.payload["question"])
            choices = list(prompt.payload["choices"])
            assert prompt.payload["kind"] == "clarify"
            assert prompt.execution_generation == 1
            assert prompt.payload["multi_select"] is False
            assert choices == ["Red (Recommended)", "Blue"]
            assert "Which colour do you prefer, red or blue?" in question

            spoken = redact_spoken(question)
            assert "Which colour do you prefer, red or blue?" in spoken
            assert "skip" in spoken

            path = f"/v1/runs/{admission.run_id}/clarify"
            wrong, wrong_body = await poster.post(
                path,
                {
                    "request_id": str(prompt.prompt_id),
                    "execution_generation": 1,
                    "choice": "Red",
                },
            )
            assert wrong.status_code == 400
            assert wrong_body["error"]["code"] == "invalid_params"
            assert str(prompt.prompt_id) in stub.controls

            response, body = await poster.post(
                path,
                {
                    "request_id": str(prompt.prompt_id),
                    "execution_generation": 1,
                    "answer": "Red",
                },
            )
            assert response.status_code == 200
            assert body == {
                "run_id": admission.run_id,
                "status": "resolved",
                "prompt_id": prompt.prompt_id,
            }
            assert stub.answers == [
                {
                    "kind": "clarify",
                    "request_id": prompt.prompt_id,
                    "execution_generation": 1,
                    "answer": "Red",
                }
            ]
            assert stub.controls == {}

            await asyncio.wait_for(collector, timeout=_STREAM_WAIT_S)
            completed_tool = [e for e in events if e.kind is EventKind.TOOL_COMPLETED]
            assert len(completed_tool) == 1
            assert completed_tool[0].tool == "clarify"
            assert events[-1].kind is EventKind.RUN_COMPLETED
            assert events[-1].output == "Red"

            status = await client.get_run(admission.run_id)
            assert status.status == "completed"
            assert status.pending_controls == ()

            repeat, repeat_body = await poster.post(
                path,
                {
                    "request_id": str(prompt.prompt_id),
                    "execution_generation": 1,
                    "answer": "Red",
                },
            )
            assert repeat.status_code == 409
            assert repeat_body["error"]["code"] == "approval_not_pending"
            assert len(stub.answers) == 1
            trace.note("clarify question spoken, answer posted once")
        finally:
            await poster.aclose()
            await client.aclose()

    with trace_guard(trace):
        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Run failure during narration
# ---------------------------------------------------------------------------


def test_run_failure_during_narration_stops_speech_and_rebases(
    gateway: Callable[..., ControlGateway],
) -> None:
    stub = gateway("hermes_run_failed_during_narration.jsonl")
    trace = EventTrace()
    clock = FakeClock()

    async def scenario() -> None:
        client = _client(stub)
        narrator = ToolNarrator(clock=clock)
        try:
            admission = await client.submit_run("run the command")
            events: list[RunEvent] = []
            narration: list[str] = []
            async for event in client.events(admission.run_id):
                trace.event(event)
                events.append(event)
                update = narrator.observe(event)
                if update is not None:
                    narration.append(update.text)

            assert [event.kind for event in events] == [
                EventKind.TOOL_STARTED,
                EventKind.TOOL_STARTED,
                EventKind.RUN_FAILED,
            ]
            assert events[-1].error_message == (
                "The turn did not complete.\nUse /retry to try again."
            )
            assert narration == ["Running a command."]
            snapshot = narrator.snapshot()
            assert snapshot.emitted == 1
            assert snapshot.suppressed == 1
            assert snapshot.last_text == "Running a command."

            failure_phrase = delegation_failure()
            assert failure_phrase == "Sorry, that task didn't finish."
            assert redact_spoken(failure_phrase) == failure_phrase
            assert all("did not complete" not in text for text in narration)
            trace.note(f"spoken failure phrase={failure_phrase!r}")

            clock.advance(0.5)
            next_run = RunEvent(
                kind=EventKind.TOOL_STARTED,
                raw_name="tool.started",
                seq=0,
                run_id="run_after_failure",
                timestamp=1793611504.0,
                payload={
                    "tool": "read_file",
                    "preview": "",
                    "tool_call_id": "call_after_failure",
                },
            )
            update = narrator.observe(next_run)
            assert update is not None
            assert update.text == "Reading the file."
            assert update.emit_at == 0.5
            trace.note("narration rebased after the terminal failure")
        finally:
            await client.aclose()

    with trace_guard(trace):
        asyncio.run(scenario())
