"""Loopback stub of the Hermes run API used by the adapter tests.

This module is deliberately not named ``test_*`` so pytest does not collect it
as a test module.  It runs a real uvicorn server in a background thread on an
ephemeral ``127.0.0.1`` port, which lets the adapter exercise real HTTP and SSE
transport behaviour without a running Hermes gateway.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

_FIXTURES = Path(__file__).parent / "fixtures"
_TOOL_TURN = _FIXTURES / "hermes_run_tool_turn.jsonl"
_CANCELLED = _FIXTURES / "hermes_run_cancelled.jsonl"
_RUN_ID = "run_fixture00000000000000000000000001"
_TERMINAL_NAMES = frozenset({"run.completed", "run.cancelled", "run.failed"})


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            frames.append(json.loads(line))
    return frames


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


class HermesStub:
    """A configurable in-process Hermes gateway double."""

    def __init__(self, api_key: str = "stub-test-key-0123456789") -> None:
        self.api_key = api_key
        self.run_id = _RUN_ID
        self.frames = _load_jsonl(_TOOL_TURN)
        self.cancelled_frames = _load_jsonl(_CANCELLED)
        self.foreign_sessions: set[str] = set()
        self.events_gone = False
        self.drop_after_seq: int | None = None
        self.emit_keepalive_every_s: float | None = None
        self.hold_open = False
        self.malformed_frame = False
        self.resend_seq: int | None = None
        self.duplicate_terminal = False
        self.initial_status = "running"
        self.initial_output = ""
        self.requests: list[dict[str, Any]] = []
        self.runs: dict[str, dict[str, Any]] = {}
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

    def make_frame(self, event: str, seq: int, **payload: Any) -> dict[str, Any]:
        """Build a fixture-shaped frame for a custom scenario."""
        data = {
            "event": event,
            "run_id": self.run_id,
            "timestamp": 1793616000.0 + seq,
            "seq": seq,
            **payload,
        }
        return {"id": seq, "data": data}

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
            "stop_event": asyncio.Event(),
            "cancelled_emitted": False,
        }

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/v1/runs")
        async def submit_run(request: Request) -> JSONResponse:
            if not self._authorized(request):
                return _auth_error()
            body = await request.json()
            self._record(request, body)

            session_id = body.get("session_id")
            if session_id in self.foreign_sessions:
                return _error("permission_denied", "permission_denied", 409)

            self.runs[self.run_id] = self._new_run(session_id)
            headers: dict[str, str] = {}
            session_key = request.headers.get("x-hermes-session-key")
            if session_key is not None:
                headers["X-Hermes-Session-Key"] = session_key
            return JSONResponse(
                {"run_id": self.run_id, "status": "started", "replayed": False},
                status_code=202,
                headers=headers,
            )

        @app.get("/v1/runs/{run_id}/events")
        async def run_events(run_id: str, request: Request) -> Any:
            if not self._authorized(request):
                return _auth_error()

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
            frames = list(self.frames)

            async def generate() -> Any:
                yield ": open\n\n"
                if self.malformed_frame:
                    yield "id: 0\ndata: {not-json}\n\n"
                    return

                emitted = resume
                resent = False
                for frame in frames:
                    seq = int(frame["id"])
                    if seq <= resume:
                        continue
                    if (
                        self.drop_after_seq is not None
                        and resume < self.drop_after_seq
                        and seq > self.drop_after_seq
                    ):
                        return

                    data = frame["data"]
                    if self.hold_open and data.get("event") in _TERMINAL_NAMES:
                        continue

                    yield self._sse(frame)
                    emitted = max(emitted, seq)
                    if self.resend_seq == seq and not resent:
                        yield self._sse(frame)
                        resent = True

                    if data.get("event") in _TERMINAL_NAMES:
                        if self.duplicate_terminal:
                            yield self._sse(frame)
                        self._settle(run, frame)
                        yield ": stream closed\n\n"
                        return

                if not self.hold_open:
                    return

                while not run["stop_event"].is_set():
                    if self.emit_keepalive_every_s is None:
                        await run["stop_event"].wait()
                        break
                    try:
                        await asyncio.wait_for(
                            run["stop_event"].wait(),
                            timeout=self.emit_keepalive_every_s,
                        )
                    except TimeoutError:
                        yield ": keepalive\n\n"

                if run["cancelled_emitted"]:
                    return
                run["cancelled_emitted"] = True
                cancelled = dict(self.cancelled_frames[0]["data"])
                next_seq = emitted + 1
                cancelled["run_id"] = run_id
                cancelled["seq"] = next_seq
                cancelled["timestamp"] = time.time()
                frame = {"id": next_seq, "data": cancelled}
                yield self._sse(frame)
                self._settle(run, frame)
                yield ": stream closed\n\n"

            return StreamingResponse(generate(), media_type="text/event-stream")

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
                    "pending_controls": [],
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

            if run["status"] in _TERMINAL_NAMES:
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
    def _sse(frame: dict[str, Any]) -> str:
        data = json.dumps(frame["data"], separators=(",", ":"))
        return f"id: {frame['id']}\ndata: {data}\n\n"

    @staticmethod
    def _settle(run: dict[str, Any], frame: dict[str, Any]) -> None:
        event = frame["data"].get("event")
        if event == "run.completed":
            run["status"] = "completed"
        elif event == "run.cancelled":
            run["status"] = "cancelled"
        elif event == "run.failed":
            run["status"] = "failed"
        run["output"] = str(frame["data"].get("output", ""))


def make_stub(**settings: Any) -> HermesStub:
    """Build a stub with attribute overrides applied before it starts."""
    stub = HermesStub()
    for name, value in settings.items():
        setattr(stub, name, value)
    return stub


__all__ = ["HermesStub", "make_stub"]
