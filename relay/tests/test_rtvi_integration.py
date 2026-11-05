"""In-process RTVI integration tests over a real aiortc peer connection.

A uvicorn server runs on a loopback port in a background thread while a real
``aiortc`` client connects to it.  Speech, the voice-brain router, Hermes, and
delegation are local fakes, so nothing leaves the machine and no model files
are loaded.  The relay's own audio bridge and outbound Opus track are real.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import suppress
from fractions import Fraction
from functools import lru_cache
from typing import Any

import httpx
import numpy as np
import pytest
import uvicorn
from fastapi import FastAPI
from relay.app import create_app
from relay.brain.delegation import DelegationHandle, DelegationState, SpokenUpdate
from relay.brain.router import RouterDecision
from relay.config import Settings
from relay.hermes.client import RunAdmission, StopResult
from relay.hermes.events import EventKind, RunEvent
from relay.speech.audio import OpusEncoder, PcmChunk
from relay.speech.stt import TranscriptEvent
from relay.speech.tts import AudioChunk
from relay.transport.rtvi import RtviSessionManager

aiortc = pytest.importorskip("aiortc")
av = pytest.importorskip("av")

_LOOPBACK_HOST = "127.0.0.1"
_TOKEN = "rtvi-integration-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_RUN_ID = "run-rtvi-integration-0001"
_DELEGATION_ID = "dlg-rtvi-integration-0001"
_PROMPT_ID = "prompt-rtvi-0001"
_FRAME_SAMPLES = 960
_FRAME_SECONDS = 0.02
_OUTBOUND_AUDIO_S = 0.2
_MAX_WAIT_S = 8.0


@lru_cache(maxsize=32)
def _opus_packets(text: str) -> tuple[bytes, ...]:
    """Encode a short 48 kHz tone once per spoken text."""
    del text
    t = np.arange(int(24000 * _OUTBOUND_AUDIO_S)) / 24000.0
    samples = (3000.0 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.int16)
    pcm = PcmChunk(data=samples.tobytes(), sample_rate=24000)
    encoder = OpusEncoder(bit_rate=32000)
    try:
        packets = encoder.encode(pcm)
        packets.extend(encoder.flush())
    finally:
        encoder.close()
    return tuple(packets)


def _event(
    kind: EventKind,
    seq: int,
    *,
    payload: Mapping[str, Any] | None = None,
) -> RunEvent:
    return RunEvent(
        kind=kind,
        raw_name=kind.value,
        seq=seq,
        run_id=_RUN_ID,
        timestamp=0.0,
        payload=payload or {},
    )


def _decision(tool: str, spoken: str, arguments: Mapping[str, Any] | None = None) -> RouterDecision:
    return RouterDecision(
        tool=tool,
        arguments=arguments or {},
        spoken_text=spoken,
        source="policy",
        rule=None,
        repaired=False,
        ttft_ms=None,
        total_ms=None,
    )


class _ScriptedSttSession:
    """Emit one final transcript after a fixed number of microphone chunks."""

    def __init__(self, text: str, after_chunks: int) -> None:
        self._text = text
        self._after = after_chunks
        self._seen = 0
        self._emitted = False
        self.peak = 0
        self.closed = False

    async def feed(self, chunk: PcmChunk) -> tuple[TranscriptEvent, ...]:
        self._seen += 1
        samples = np.frombuffer(chunk.data, dtype=np.int16)
        if samples.size:
            self.peak = max(self.peak, int(np.max(np.abs(samples.astype(np.int32)))))
        if self._emitted or self._seen < self._after:
            return ()
        self._emitted = True
        return (
            TranscriptEvent(
                kind="final",
                text=self._text,
                engine="scripted",
                latency_ms=0.0,
                audio_ms=chunk.duration_ms,
            ),
        )

    async def flush(self) -> TranscriptEvent | None:
        return None

    async def close(self) -> None:
        self.closed = True


class _ScriptedSttAdapter:
    name = "scripted"

    def __init__(self, text: str, after_chunks: int = 3) -> None:
        self._text = text
        self._after = after_chunks
        self.sessions: list[_ScriptedSttSession] = []

    def describe(self) -> dict[str, object]:
        return {"engine": "scripted", "streaming": True}

    async def start_session(self) -> _ScriptedSttSession:
        session = _ScriptedSttSession(self._text, self._after)
        self.sessions.append(session)
        return session


class _ScriptedTts:
    """Yield one real Opus chunk per reply and record spoken text."""

    def __init__(self, texts: list[str], cleared: list[int]) -> None:
        self._texts = texts
        self._cleared = cleared
        self._text = ""
        self.closed = False

    async def push_text(self, text: str) -> None:
        self._texts.append(text)
        self._text = text

    async def flush(self) -> None:
        return None

    async def chunks(self) -> AsyncIterator[AudioChunk]:
        packets = _opus_packets(self._text)
        yield AudioChunk(
            opus_packets=packets,
            duration_ms=_OUTBOUND_AUDIO_S * 1000.0,
            sentence_index=0,
            text=self._text,
            first_audio_ms=0.0,
        )

    def clear(self) -> None:
        self._cleared.append(1)

    async def close(self) -> None:
        self.closed = True


class _ScriptedRouter:
    def __init__(self, decision: RouterDecision) -> None:
        self._decision = decision
        self.utterances: list[str] = []

    async def decide(self, utterance: str) -> RouterDecision:
        self.utterances.append(utterance)
        return self._decision


class _FakeHermes:
    """Hermes double with a scripted run-event stream and control hooks."""

    def __init__(
        self,
        scenario: Callable[[_FakeHermes, str], AsyncIterator[RunEvent]] | None = None,
    ) -> None:
        self._scenario = scenario
        self.run_id = _RUN_ID
        self.stops: list[str] = []
        self.approvals: list[dict[str, Any]] = []
        self.stop_event = asyncio.Event()
        self.approval_event = asyncio.Event()
        self.closed = False

    async def submit_run(self, *args: Any, **kwargs: Any) -> RunAdmission:
        del args, kwargs
        return RunAdmission(
            run_id=self.run_id,
            status="started",
            replayed=False,
            session_key=None,
        )

    async def events(self, run_id: str, **kwargs: Any) -> AsyncIterator[RunEvent]:
        del kwargs
        if self._scenario is None:
            return
        async for event in self._scenario(self, run_id):
            yield event

    async def stop(self, run_id: str) -> StopResult:
        self.stops.append(run_id)
        self.stop_event.set()
        return StopResult(
            run_id=run_id,
            status="stopping",
            admission_id=None,
            elapsed_s=0.0,
        )

    async def approval(self, run_id: str, **kwargs: Any) -> None:
        self.approvals.append({"run_id": run_id, **kwargs})
        self.approval_event.set()

    async def aclose(self) -> None:
        self.closed = True


class _FakeDelegation:
    """Delegation double that consumes the tee'd event stream and delivers one update."""

    def __init__(
        self,
        client: Any,
        deliver: Callable[[SpokenUpdate], Any],
        completion_text: str,
    ) -> None:
        self._client = client
        self._deliver = deliver
        self._completion_text = completion_text
        self._watcher: asyncio.Task[None] | None = None
        self._closed = False

    async def delegate(
        self,
        instruction: str,
        *,
        session_id: str | None = None,
        session_key: str | None = None,
        **kwargs: Any,
    ) -> DelegationHandle:
        del session_id, session_key, kwargs
        admission = await self._client.submit_run(instruction)
        run_id = admission.run_id
        self._watcher = asyncio.create_task(self._watch(run_id), name="fake.delegation")
        return DelegationHandle(
            delegation_id=_DELEGATION_ID,
            idempotency_key="dlg-test",
            run_id=run_id,
            state=DelegationState.RUNNING,
            spoken_ack="On it.",
            deduplicated=False,
            replayed=False,
            enqueue_ms=0.0,
            failure=None,
        )

    async def _watch(self, run_id: str) -> None:
        cancelled = False
        async for event in self._client.events(run_id):
            if event.kind is EventKind.RUN_CANCELLED:
                cancelled = True
        text = "Stopped." if cancelled else self._completion_text
        kind = "cancelled" if cancelled else "answer"
        await self._deliver(
            SpokenUpdate(
                delegation_id=_DELEGATION_ID,
                run_id=run_id,
                kind=kind,
                text=text,
                final=True,
            )
        )

    async def aclose(self) -> None:
        self._closed = True
        watcher = self._watcher
        self._watcher = None
        if watcher is not None:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher


class _RelayHarness:
    """Own a uvicorn server with injected RTVI fakes for one test."""

    def __init__(
        self,
        *,
        router: Any,
        stt: _ScriptedSttAdapter,
        hermes: _FakeHermes,
        delegation_factory: Callable[[Any, Callable[[SpokenUpdate], Any]], Any] | None = None,
        approval_timeout_s: float = 20.0,
        call_cap_seconds: float | None = None,
        call_cap_warning_seconds: float | None = None,
    ) -> None:
        settings = Settings(
            _env_file=None,
            device_tokens_raw=_TOKEN,
            approval_timeout_s=approval_timeout_s,
        )
        app = create_app(settings)
        self.texts: list[str] = []
        self.cleared: list[int] = []
        self.app: FastAPI = app
        self.manager = RtviSessionManager(
            app,
            settings,
            app.state.limits,
            stt_factory=lambda: stt,
            tts_factory=lambda: _ScriptedTts(self.texts, self.cleared),
            router_factory=lambda: router,
            hermes_factory=lambda: hermes,
            delegation_factory=delegation_factory,
            call_cap_seconds=call_cap_seconds,
            call_cap_warning_seconds=call_cap_warning_seconds,
        )
        app.state.rtvi = self.manager
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.base_url = ""

    def start(self) -> None:
        config = uvicorn.Config(
            self.app,
            host=_LOOPBACK_HOST,
            port=0,
            log_level="error",
            lifespan="on",
            access_log=False,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()

        deadline = time.monotonic() + _MAX_WAIT_S
        while self.server is not None and not self.server.started:
            if time.monotonic() > deadline:
                msg = "RTVI test server did not start"
                raise RuntimeError(msg)
            time.sleep(0.01)

        server = self.server
        if server is None or not server.servers:
            msg = "RTVI test server has no listening socket"
            raise RuntimeError(msg)
        sock = server.servers[0].sockets[0]
        host, port = sock.getsockname()[:2]
        self.base_url = f"http://{host}:{port}"

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=_MAX_WAIT_S)


class _ClientMicTrack(aiortc.mediastreams.MediaStreamTrack):
    """A 48 kHz microphone track that can be made loud for a frame count."""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._pts = 0
        self._sample_offset = 0
        self._loud_frames = 0
        self._closed = False
        self.sent = 0
        self.stopped = False

    def speak(self, frames: int) -> None:
        self._loud_frames = frames

    async def recv(self) -> Any:
        await asyncio.sleep(_FRAME_SECONDS)
        if self._closed:
            raise aiortc.mediastreams.MediaStreamError
        if self._loud_frames > 0:
            self._loud_frames -= 1
            t = np.arange(self._sample_offset, self._sample_offset + _FRAME_SAMPLES) / 48000.0
            samples = (8000.0 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.int16)
        else:
            samples = np.zeros(_FRAME_SAMPLES, dtype=np.int16)
        self._sample_offset += _FRAME_SAMPLES
        frame = av.AudioFrame(
            format="s16",
            layout="mono",
            samples=_FRAME_SAMPLES,
        )
        frame.sample_rate = 48000
        frame.pts = self._pts
        frame.time_base = Fraction(1, 48000)
        frame.planes[0].update(samples.tobytes())
        self._pts += _FRAME_SAMPLES
        self.sent += 1
        return frame

    def stop(self) -> None:
        self._closed = True
        self.stopped = True
        super().stop()


class _Call:
    """A real aiortc client connected to the relay under test."""

    def __init__(
        self,
        base_url: str,
        messages: list[dict[str, Any]],
        audio: list[bool],
    ) -> None:
        self._base_url = base_url
        self.messages = messages
        self.audio = audio
        self.http = httpx.AsyncClient(base_url=base_url, timeout=_MAX_WAIT_S)
        self.pc: Any | None = None
        self.channel: Any | None = None
        self.mic = _ClientMicTrack()
        self.session_id = ""
        self._tasks: list[asyncio.Task[Any]] = []

    async def open(self) -> None:
        pc = aiortc.RTCPeerConnection()
        self.pc = pc
        channel = pc.createDataChannel("chat")
        self.channel = channel
        channel.on("message", self._on_message)
        pc.addTrack(self.mic)
        pc.on("track", self._on_track)

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await self._wait_for_ice(pc)

        started = await self.http.post("/start", headers=_AUTH)
        assert started.status_code == 200, started.text
        self.session_id = str(started.json()["sessionId"])

        answer = await self.http.post(
            f"/sessions/{self.session_id}/api/offer",
            headers=_AUTH,
            json={"sdp": pc.localDescription.sdp, "type": "offer"},
        )
        assert answer.status_code == 200, answer.text
        body = answer.json()
        await pc.setRemoteDescription(
            aiortc.RTCSessionDescription(sdp=body["sdp"], type=body["type"])
        )

        await _wait_for(
            lambda: channel.readyState == "open",
            timeout_s=_MAX_WAIT_S,
            description="data channel open",
        )
        await _wait_for(
            lambda: any(message.get("type") == "bot-ready" for message in self.messages),
            timeout_s=_MAX_WAIT_S,
            description="bot-ready",
        )

    async def delete(self) -> None:
        response = await self.http.delete(f"/sessions/{self.session_id}")
        assert response.status_code == 200, response.text

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        pc = self.pc
        self.pc = None
        if pc is not None:
            await pc.close()
        await self.http.aclose()

    async def _wait_for_ice(self, pc: Any) -> None:
        if pc.iceGatheringState == "complete":
            return
        gathered = asyncio.Event()

        @pc.on("icegatheringstatechange")
        def _on_icegatheringstatechange() -> None:
            if pc.iceGatheringState == "complete":
                gathered.set()

        await asyncio.wait_for(gathered.wait(), timeout=_MAX_WAIT_S)

    def _on_message(self, message: Any) -> None:
        self.messages.append(json.loads(message))

    def _on_track(self, track: Any) -> None:
        self._tasks.append(asyncio.create_task(self._consume(track), name="client.audio"))

    async def _consume(self, track: Any) -> None:
        try:
            while True:
                frame = await track.recv()
                self.audio.append(any(bytes(frame.planes[0])))
        except aiortc.mediastreams.MediaStreamError:
            return
        except asyncio.CancelledError:
            raise


async def _wait_for(
    predicate: Callable[[], bool],
    *,
    timeout_s: float,
    description: str,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    msg = f"timed out waiting for {description}"
    raise AssertionError(msg)


def _queue_size(harness: _RelayHarness, session_id: str) -> int:
    session = harness.manager._sessions.get(session_id)
    return session._mic_queue.qsize() if session is not None else -1


def _turn_state(harness: _RelayHarness, session_id: str) -> str:
    session = harness.manager._sessions.get(session_id)
    loop = session._call_loop if session is not None else None
    if loop is None:
        return "unknown"
    return str(loop._controller.snapshot().get("turn_state"))


def _controller_snapshot(harness: _RelayHarness, session_id: str) -> dict[str, Any]:
    session = harness.manager._sessions.get(session_id)
    loop = session._call_loop if session is not None else None
    if loop is None:
        return {}
    snapshot = dict(loop._controller.snapshot())
    gate = loop._controller._gate
    snapshot["gate"] = gate.snapshot() if callable(getattr(gate, "snapshot", None)) else {}
    return snapshot


def _types(messages: list[dict[str, Any]]) -> list[str]:
    return [str(message.get("type")) for message in messages]


def _find(messages: list[dict[str, Any]], message_type: str) -> list[dict[str, Any]]:
    return [message for message in messages if message.get("type") == message_type]


def _scenario_completed(hermes: _FakeHermes, run_id: str) -> AsyncIterator[RunEvent]:
    async def _stream() -> AsyncIterator[RunEvent]:
        yield _event(
            EventKind.TOOL_STARTED,
            1,
            payload={"tool": "terminal"},
        )
        await asyncio.sleep(0.1)
        yield _event(
            EventKind.RUN_COMPLETED,
            2,
            payload={"output": "All done."},
        )

    del hermes
    return _stream()


def _scenario_hanging(hermes: _FakeHermes, run_id: str) -> AsyncIterator[RunEvent]:
    async def _stream() -> AsyncIterator[RunEvent]:
        yield _event(
            EventKind.TOOL_STARTED,
            1,
            payload={"tool": "terminal"},
        )
        await hermes.stop_event.wait()
        yield _event(EventKind.RUN_CANCELLED, 2)

    del run_id
    return _stream()


def _scenario_approval(hermes: _FakeHermes, run_id: str) -> AsyncIterator[RunEvent]:
    async def _stream() -> AsyncIterator[RunEvent]:
        yield _event(
            EventKind.TOOL_STARTED,
            1,
            payload={"tool": "terminal"},
        )
        yield _event(
            EventKind.APPROVAL_REQUEST,
            2,
            payload={
                "prompt_id": _PROMPT_ID,
                "execution_generation": 1,
                "command": "rm -rf build",
                "description": "Delete the build directory",
            },
        )
        await hermes.approval_event.wait()
        yield _event(
            EventKind.RUN_COMPLETED,
            3,
            payload={"output": "All done."},
        )

    del run_id
    return _stream()


def _delegation_factory(completion_text: str) -> Callable[..., _FakeDelegation]:
    def _build(client: Any, deliver: Callable[[SpokenUpdate], Any]) -> _FakeDelegation:
        return _FakeDelegation(client, deliver, completion_text)

    return _build


def test_rtvi_direct_answer_plays_outbound_audio_and_closes() -> None:
    messages: list[dict[str, Any]] = []
    audio: list[bool] = []
    harness = _RelayHarness(
        router=_ScriptedRouter(_decision("answer_direct", "Hello from the relay.")),
        stt=_ScriptedSttAdapter("hello"),
        hermes=_FakeHermes(),
    )
    harness.start()

    async def scenario() -> None:
        call = _Call(harness.base_url, messages, audio)
        try:
            await call.open()
            call.mic.speak(4)
            await _wait_for(
                lambda: any(
                    message.get("type") == "user-transcription"
                    and message.get("data", {}).get("final") is True
                    for message in messages
                ),
                timeout_s=_MAX_WAIT_S,
                description="final transcript",
            )
            await _wait_for(
                lambda: bool(_find(messages, "bot-llm-text")),
                timeout_s=_MAX_WAIT_S,
                description="bot-llm-text",
            )
            await _wait_for(
                lambda: any(audio),
                timeout_s=_MAX_WAIT_S,
                description="nonzero outbound audio",
            )

            types = _types(messages)
            assert "bot-ready" in types
            assert "bot-tts-started" in types
            assert "bot-tts-stopped" in types
            assert _find(messages, "bot-llm-text")[0]["data"]["text"] == "Hello from the relay."
            assert "Hello from the relay." in harness.texts
            assert harness.texts.count("Hello from the relay.") == 1

            await call.delete()
            await _wait_for(
                lambda: any(
                    message.get("data", {}).get("reason") == "client_delete"
                    for message in _find(messages, "session-closed")
                ),
                timeout_s=_MAX_WAIT_S,
                description="session-closed",
            )
        finally:
            await call.close()

    try:
        asyncio.run(scenario())
    finally:
        harness.stop()


def test_rtvi_delegation_narrates_and_reports_status() -> None:
    messages: list[dict[str, Any]] = []
    audio: list[bool] = []
    harness = _RelayHarness(
        router=_ScriptedRouter(
            _decision(
                "delegate_to_hermes",
                "On it.",
                {"instruction": "run the build"},
            )
        ),
        stt=_ScriptedSttAdapter("please run the build"),
        hermes=_FakeHermes(_scenario_completed),
        delegation_factory=_delegation_factory("All done."),
    )
    harness.start()

    async def scenario() -> None:
        call = _Call(harness.base_url, messages, audio)
        try:
            await call.open()
            call.mic.speak(4)
            await _wait_for(
                lambda: any(
                    message.get("data", {}).get("state") == "completed"
                    for message in _find(messages, "delegation-status")
                ),
                timeout_s=_MAX_WAIT_S,
                description="completed delegation status",
            )

            statuses = _find(messages, "delegation-status")
            assert statuses[0]["data"]["state"] == "running"
            assert statuses[0]["data"]["run_id"] == _RUN_ID
            assert statuses[-1]["data"]["state"] == "completed"
            assert "Running a command." in harness.texts
            assert "All done." in harness.texts
        finally:
            await call.close()

    try:
        asyncio.run(scenario())
    finally:
        harness.stop()


def test_rtvi_barge_in_stops_the_run() -> None:
    messages: list[dict[str, Any]] = []
    audio: list[bool] = []
    hermes = _FakeHermes(_scenario_hanging)
    stt = _ScriptedSttAdapter("please run a long task")
    harness = _RelayHarness(
        router=_ScriptedRouter(
            _decision(
                "delegate_to_hermes",
                "On it.",
                {"instruction": "run a long task"},
            )
        ),
        stt=stt,
        hermes=hermes,
        delegation_factory=_delegation_factory("All done."),
    )
    harness.start()

    async def scenario() -> None:
        call = _Call(harness.base_url, messages, audio)
        try:
            await call.open()
            call.mic.speak(4)
            await _wait_for(
                lambda: bool(_find(messages, "delegation-status")),
                timeout_s=_MAX_WAIT_S,
                description="delegation status",
            )
            call.mic.speak(20)
            try:
                await _wait_for(
                    lambda: bool(_find(messages, "barge-in")),
                    timeout_s=_MAX_WAIT_S,
                    description="barge-in",
                )
            except AssertionError:
                msg = (
                    f"barge-in missing (saw {_types(messages)}; "
                    f"stt seen={stt.sessions[0]._seen} peak={stt.sessions[0].peak}; "
                    f"mic sent={call.mic.sent} loud={call.mic._loud_frames}; "
                    f"queue={_queue_size(harness, call.session_id)}; "
                    f"turn={_turn_state(harness, call.session_id)}; "
                    f"controller={_controller_snapshot(harness, call.session_id)}; "
                    f"pc={call.pc.connectionState})"
                )
                raise AssertionError(msg) from None

            barge_in = _find(messages, "barge-in")[0]["data"]
            assert barge_in["run_id"] == _RUN_ID
            assert barge_in["stop_ok"] is True
            assert hermes.stops == [_RUN_ID]
            assert _find(messages, "bot-tts-stopped")
        finally:
            await call.close()

    try:
        asyncio.run(scenario())
    finally:
        harness.stop()


def test_rtvi_approval_times_out_and_posts_deny() -> None:
    messages: list[dict[str, Any]] = []
    audio: list[bool] = []
    hermes = _FakeHermes(_scenario_approval)
    harness = _RelayHarness(
        router=_ScriptedRouter(
            _decision(
                "delegate_to_hermes",
                "On it.",
                {"instruction": "delete the build directory"},
            )
        ),
        stt=_ScriptedSttAdapter("delete the build directory"),
        hermes=hermes,
        delegation_factory=_delegation_factory("All done."),
        approval_timeout_s=0.2,
    )
    harness.start()

    async def scenario() -> None:
        call = _Call(harness.base_url, messages, audio)
        try:
            await call.open()
            call.mic.speak(4)
            await _wait_for(
                lambda: bool(_find(messages, "approval-request")),
                timeout_s=_MAX_WAIT_S,
                description="approval-request",
            )
            request = _find(messages, "approval-request")[0]["data"]
            assert request["prompt_id"] == _PROMPT_ID
            assert request["execution_generation"] == 1
            assert "rm -rf build" in request["text"]
            assert "yes run it" in request["text"]
            assert 0.0 <= request["deadline_s"] <= 0.2

            await _wait_for(
                lambda: bool(_find(messages, "approval-decision")),
                timeout_s=_MAX_WAIT_S,
                description="approval-decision",
            )
            decision = _find(messages, "approval-decision")[0]["data"]
            assert decision == {
                "prompt_id": _PROMPT_ID,
                "choice": "deny",
                "reason": "timeout",
            }
            assert hermes.approvals == [
                {
                    "run_id": _RUN_ID,
                    "request_id": _PROMPT_ID,
                    "execution_generation": 1,
                    "choice": "deny",
                }
            ]
        finally:
            await call.close()

    try:
        asyncio.run(scenario())
    finally:
        harness.stop()


def test_rtvi_call_cap_warns_and_closes_the_session() -> None:
    messages: list[dict[str, Any]] = []
    audio: list[bool] = []
    harness = _RelayHarness(
        router=_ScriptedRouter(_decision("answer_direct", "OK.")),
        stt=_ScriptedSttAdapter("hello"),
        hermes=_FakeHermes(),
        call_cap_seconds=0.6,
        call_cap_warning_seconds=0.2,
    )
    harness.start()

    async def scenario() -> None:
        call = _Call(harness.base_url, messages, audio)
        try:
            await call.open()
            await _wait_for(
                lambda: bool(_find(messages, "call-cap-warning")),
                timeout_s=_MAX_WAIT_S,
                description="call-cap-warning",
            )
            warning = _find(messages, "call-cap-warning")[0]["data"]
            assert warning == {"seconds_left": 1}
            await _wait_for(
                lambda: any(
                    message.get("data", {}).get("reason") == "call_cap"
                    for message in _find(messages, "session-closed")
                ),
                timeout_s=_MAX_WAIT_S,
                description="call-cap session close",
            )
            assert "Time's up. Goodbye." in harness.texts
        finally:
            await call.close()

    try:
        asyncio.run(scenario())
    finally:
        harness.stop()


def test_rtvi_unknown_session_is_rejected_by_the_live_server() -> None:
    harness = _RelayHarness(
        router=_ScriptedRouter(_decision("answer_direct", "OK.")),
        stt=_ScriptedSttAdapter("hello"),
        hermes=_FakeHermes(),
    )
    harness.start()

    async def scenario() -> None:
        async with httpx.AsyncClient(base_url=harness.base_url, timeout=_MAX_WAIT_S) as client:
            response = await client.post(
                f"/sessions/{uuid.uuid4()}/api/offer",
                headers=_AUTH,
                json={"sdp": "v=0", "type": "offer"},
            )
            assert response.status_code == 404
            assert response.json() == {"error": "unknown session"}

    try:
        asyncio.run(scenario())
    finally:
        harness.stop()
