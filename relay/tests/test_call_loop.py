"""Offline tests for the transport-agnostic RTVI call loop.

Every dependency (STT, TTS, router, Hermes client, delegation) is a local fake.
No sockets, audio devices, model files, or network calls are involved.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import numpy as np
import pytest
from relay.brain.delegation import DelegationState
from relay.brain.router import RouterDecision
from relay.config import Settings
from relay.hermes.client import RunAdmission, StopResult
from relay.hermes.events import EventKind, RunEvent
from relay.speech.audio import PcmChunk
from relay.speech.stt import TranscriptEvent
from relay.speech.tts import AudioChunk
from relay.transport.call_loop import (
    CALL_CAP_GOODBYE,
    CALL_CAP_WARNING_TEXT,
    CallLoop,
)
from relay.transport.turns import TurnState

_SAMPLE_RATE = 16000
_FRAME_MS = 20
_RUN_ID = "run-call-loop-0001"
_APPROVAL_TIMEOUT_S = 5.0


def _transcript(text: str) -> TranscriptEvent:
    return TranscriptEvent(
        kind="final",
        text=text,
        engine="fake",
        latency_ms=0.0,
        audio_ms=float(_FRAME_MS),
    )


def _event(
    kind: EventKind,
    seq: int,
    *,
    run_id: str = _RUN_ID,
    payload: dict[str, Any] | None = None,
) -> RunEvent:
    return RunEvent(
        kind=kind,
        raw_name=kind.value,
        seq=seq,
        run_id=run_id,
        timestamp=0.0,
        payload=payload or {},
    )


def _decision(tool: str, spoken: str, arguments: dict[str, Any] | None = None) -> RouterDecision:
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


class _FakeTts:
    """Minimal StreamingTts double that records text and clears."""

    def __init__(self, texts: list[str], cleared: list[int]) -> None:
        self._texts = texts
        self._cleared = cleared
        self.closed = False

    async def push_text(self, text: str) -> None:
        self._texts.append(text)

    async def flush(self) -> None:
        return None

    async def chunks(self) -> AsyncIterator[AudioChunk]:
        yield AudioChunk(
            opus_packets=(b"fake-opus",),
            duration_ms=float(_FRAME_MS),
            sentence_index=0,
            text="",
            first_audio_ms=0.0,
        )

    def clear(self) -> None:
        self._cleared.append(1)

    async def close(self) -> None:
        self.closed = True


class _FakeRouter:
    def __init__(self, decision: RouterDecision) -> None:
        self._decision = decision
        self.utterances: list[str] = []

    async def decide(self, utterance: str) -> RouterDecision:
        self.utterances.append(utterance)
        return self._decision


class _FakeHermes:
    def __init__(
        self,
        *,
        event_factory: Callable[[str], AsyncIterator[RunEvent]] | None = None,
        run_id: str = _RUN_ID,
    ) -> None:
        self._event_factory = event_factory
        self._run_id = run_id
        self.submissions: list[dict[str, Any]] = []
        self.stops: list[str] = []
        self.approvals: list[dict[str, Any]] = []
        self.closed = False

    async def submit_run(self, *args: Any, **kwargs: Any) -> RunAdmission:
        self.submissions.append({"args": args, "kwargs": kwargs})
        return RunAdmission(
            run_id=self._run_id,
            status="started",
            replayed=False,
            session_key=None,
        )

    async def events(self, run_id: str, **kwargs: Any) -> AsyncIterator[RunEvent]:
        if self._event_factory is None:
            return
        async for event in self._event_factory(run_id):
            yield event

    async def stop(self, run_id: str) -> StopResult:
        self.stops.append(run_id)
        return StopResult(
            run_id=run_id,
            status="stopping",
            admission_id=None,
            elapsed_s=0.0,
        )

    async def approval(self, run_id: str, **kwargs: Any) -> None:
        self.approvals.append({"run_id": run_id, **kwargs})

    async def aclose(self) -> None:
        self.closed = True


class _FakeSttSession:
    async def feed(self, chunk: PcmChunk) -> tuple[TranscriptEvent, ...]:
        return ()

    async def close(self) -> None:
        return None


class _FakeSttAdapter:
    async def start_session(self) -> _FakeSttSession:
        return _FakeSttSession()


class _FakeTimer:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _FakeScheduler:
    """Capture scheduled callbacks so tests can fire them deterministically."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, Callable[[], None]]] = []
        self.timers: list[_FakeTimer] = []

    def __call__(self, delay: float, callback: Callable[[], None]) -> _FakeTimer:
        self.calls.append((delay, callback))
        timer = _FakeTimer()
        self.timers.append(timer)
        return timer

    def fire(self, index: int) -> None:
        self.calls[index][1]()


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"_env_file": None}
    values.update(overrides)
    return Settings(**values)


def _build_loop(
    *,
    decision: RouterDecision,
    hermes: _FakeHermes | None = None,
    settings: Settings | None = None,
    scheduler: _FakeScheduler | None = None,
    on_close: Callable[[str], Any] | None = None,
    call_cap_seconds: float | None = 0.0,
    call_cap_warning_seconds: float | None = None,
) -> tuple[CallLoop, list[dict[str, Any]], list[AudioChunk], list[str], list[int]]:
    messages: list[dict[str, Any]] = []
    audio: list[AudioChunk] = []
    texts: list[str] = []
    cleared: list[int] = []
    resolved_hermes = hermes if hermes is not None else _FakeHermes()
    loop = CallLoop(
        settings if settings is not None else _settings(),
        emit_message=messages.append,
        emit_audio=audio.append,
        tts_factory=lambda: _FakeTts(texts, cleared),
        router=_FakeRouter(decision),
        hermes=resolved_hermes,
        stt_adapter=_FakeSttAdapter(),
        outbound_clear=lambda: cleared.append(1),
        scheduler=scheduler,
        call_cap_seconds=call_cap_seconds,
        call_cap_warning_seconds=call_cap_warning_seconds,
        on_close=on_close,
    )
    return loop, messages, audio, texts, cleared


def _message_types(messages: list[dict[str, Any]]) -> list[str]:
    return [str(message.get("type")) for message in messages]


def _find(messages: list[dict[str, Any]], message_type: str) -> list[dict[str, Any]]:
    return [message for message in messages if message.get("type") == message_type]


async def _wait_for(
    predicate: Callable[[], bool],
    *,
    timeout_s: float = 2.0,
    description: str = "condition",
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    msg = f"timed out waiting for {description}"
    raise AssertionError(msg)


def _loud_chunk(amplitude: int = 16000) -> PcmChunk:
    samples = np.full(_SAMPLE_RATE * _FRAME_MS // 1000, amplitude, dtype=np.int16)
    return PcmChunk(data=samples.tobytes(), sample_rate=_SAMPLE_RATE)


def test_direct_answer_speaks_and_completes_turn() -> None:
    async def run() -> None:
        loop, messages, audio, texts, _ = _build_loop(
            decision=_decision("answer_direct", "Hello there.", {"text": "Hello there."}),
        )
        await loop.start()
        await loop.handle_transcript(_transcript("hello"))
        await loop.wait_idle()

        types = _message_types(messages)
        assert types[0] == "user-transcription"
        transcription = _find(messages, "user-transcription")[0]
        assert transcription["data"] == {"text": "hello", "final": True}
        assert _find(messages, "bot-llm-text")[0]["data"]["text"] == "Hello there."
        assert "bot-tts-started" in types
        assert "bot-tts-stopped" in types
        assert texts == ["Hello there."]
        assert len(audio) == 1
        assert loop._controller.snapshot()["turn_state"] == TurnState.LISTENING.value
        await loop.close("test")

    asyncio.run(run())


def test_delegated_turn_narrates_and_reports_status() -> None:
    async def events(run_id: str) -> AsyncIterator[RunEvent]:
        yield _event(
            EventKind.TOOL_STARTED,
            1,
            run_id=run_id,
            payload={"tool": "terminal"},
        )
        yield _event(
            EventKind.RUN_COMPLETED,
            2,
            run_id=run_id,
            payload={"output": "All done."},
        )

    async def run() -> None:
        hermes = _FakeHermes(event_factory=events)
        loop, messages, _, texts, _ = _build_loop(
            decision=_decision("delegate_to_hermes", "On it.", {"instruction": "run it"}),
            hermes=hermes,
        )
        await loop.start()
        await loop.handle_transcript(_transcript("please run it"))
        await _wait_for(
            lambda: any(
                message["data"].get("state") == "completed"
                for message in _find(messages, "delegation-status")
            ),
            description="completed delegation status",
        )
        await loop.wait_idle()

        statuses = _find(messages, "delegation-status")
        assert statuses[0]["data"]["state"] == DelegationState.RUNNING.value
        assert statuses[0]["data"]["run_id"] == _RUN_ID
        assert statuses[-1]["data"]["state"] == DelegationState.COMPLETED.value
        assert "Running a command." in texts
        assert "All done." in texts
        assert hermes.submissions
        await loop.close("test")

    asyncio.run(run())


def test_barge_in_stops_run_and_flushes_playback() -> None:
    async def events(run_id: str) -> AsyncIterator[RunEvent]:
        yield _event(
            EventKind.TOOL_STARTED,
            1,
            run_id=run_id,
            payload={"tool": "terminal"},
        )
        await asyncio.Event().wait()

    async def run() -> None:
        hermes = _FakeHermes(event_factory=events)
        loop, messages, _, _, cleared = _build_loop(
            decision=_decision("delegate_to_hermes", "On it.", {"instruction": "run it"}),
            hermes=hermes,
        )
        await loop.start()
        await loop.handle_transcript(_transcript("please run it"))
        await _wait_for(
            lambda: loop._controller.snapshot()["turn_state"] == TurnState.SPEAKING.value,
            description="speaking turn",
        )

        for _ in range(30):
            await loop.handle_mic(_loud_chunk())
            if hermes.stops:
                break
        await loop.wait_idle()

        assert hermes.stops == [_RUN_ID]
        barge_in = _find(messages, "barge-in")
        assert barge_in
        assert barge_in[0]["data"] == {
            "turn_id": barge_in[0]["data"]["turn_id"],
            "run_id": _RUN_ID,
            "stop_ok": True,
        }
        assert _find(messages, "bot-tts-stopped")
        assert cleared
        assert loop._controller.snapshot()["turn_state"] == TurnState.LISTENING.value
        await loop.close("test")

    asyncio.run(run())


def test_approval_silence_expires_and_posts_deny() -> None:
    async def run() -> None:
        hermes = _FakeHermes()
        scheduler = _FakeScheduler()
        loop, messages, _, texts, _ = _build_loop(
            decision=_decision("answer_direct", "OK."),
            hermes=hermes,
            settings=_settings(approval_timeout_s=_APPROVAL_TIMEOUT_S),
            scheduler=scheduler,
        )
        await loop.start()
        await loop.handle_run_event(
            _event(
                EventKind.TOOL_STARTED,
                1,
                payload={"tool": "terminal"},
            )
        )
        await loop.handle_run_event(
            _event(
                EventKind.APPROVAL_REQUEST,
                2,
                payload={
                    "tool": "terminal",
                    "command": "rm -rf build",
                    "description": "Delete the build directory",
                    "prompt_id": "prompt-1",
                    "execution_generation": 7,
                },
            )
        )
        await loop.wait_idle()

        requests = _find(messages, "approval-request")
        assert requests
        assert requests[0]["data"]["prompt_id"] == "prompt-1"
        assert requests[0]["data"]["execution_generation"] == 7
        assert "rm -rf build" in requests[0]["data"]["text"]
        assert "yes run it" in requests[0]["data"]["text"]
        assert any("Delete the build directory" in text for text in texts)
        assert scheduler.calls

        scheduler.fire(0)
        await loop.wait_idle()

        decisions = _find(messages, "approval-decision")
        assert decisions
        assert decisions[0]["data"] == {
            "prompt_id": "prompt-1",
            "choice": "deny",
            "reason": "timeout",
        }
        assert hermes.approvals == [
            {
                "run_id": _RUN_ID,
                "request_id": "prompt-1",
                "execution_generation": 7,
                "choice": "deny",
            }
        ]
        await loop.close("test")

    asyncio.run(run())


def test_call_cap_warns_then_closes_through_injected_scheduler() -> None:
    async def run() -> None:
        scheduler = _FakeScheduler()
        closed: list[str] = []
        loop, messages, audio, texts, cleared = _build_loop(
            decision=_decision("answer_direct", "OK."),
            scheduler=scheduler,
            on_close=closed.append,
            call_cap_seconds=10.0,
            call_cap_warning_seconds=3.0,
        )
        await loop.start()
        assert [delay for delay, _ in scheduler.calls] == [7.0, 10.0]

        scheduler.fire(0)
        await loop.wait_idle()
        warning = _find(messages, "call-cap-warning")
        assert warning
        assert warning[0]["data"] == {"seconds_left": 3}
        assert CALL_CAP_WARNING_TEXT.format(seconds=3) in texts
        audio_after_warning = len(audio)

        scheduler.fire(1)
        await loop.wait_idle()
        assert closed == ["call_cap"]
        assert CALL_CAP_GOODBYE in texts
        assert len(audio) > audio_after_warning
        assert audio[-1].opus_packets == (b"fake-opus",)
        assert cleared == []
        await loop.close("test")

    asyncio.run(run())


def test_call_cap_defaults_to_max_call_minutes_setting() -> None:
    async def run() -> None:
        scheduler = _FakeScheduler()
        loop, _, _, _, _ = _build_loop(
            decision=_decision("answer_direct", "OK."),
            settings=_settings(max_call_minutes=1),
            scheduler=scheduler,
            call_cap_seconds=None,
        )
        await loop.start()
        assert [delay for delay, _ in scheduler.calls] == [30.0, 60.0]
        await loop.close("test")

    asyncio.run(run())


def test_concurrent_speech_paths_are_serialized() -> None:
    class _RecordingTts:
        """TTS double that records enter/exit pairs and detects overlap."""

        def __init__(self, state: dict[str, Any]) -> None:
            self._state = state
            self._text = ""

        async def push_text(self, text: str) -> None:
            self._text = text
            self._state["texts"].append(text)
            self._state["active"] += 1
            self._state["max_active"] = max(self._state["max_active"], self._state["active"])
            self._state["events"].append(("enter", text))
            await asyncio.sleep(0.05)

        async def flush(self) -> None:
            await asyncio.sleep(0.02)

        async def chunks(self) -> AsyncIterator[AudioChunk]:
            await asyncio.sleep(0.02)
            yield AudioChunk(
                opus_packets=(b"fake-opus",),
                duration_ms=float(_FRAME_MS),
                sentence_index=0,
                text="",
                first_audio_ms=0.0,
            )

        async def close(self) -> None:
            self._state["active"] -= 1
            self._state["events"].append(("exit", self._text))

    async def run() -> None:
        state: dict[str, Any] = {
            "texts": [],
            "events": [],
            "active": 0,
            "max_active": 0,
        }
        loop = CallLoop(
            _settings(),
            emit_message=lambda message: None,
            emit_audio=lambda chunk: None,
            tts_factory=lambda: _RecordingTts(state),
            router=_FakeRouter(_decision("answer_direct", "OK.")),
            hermes=_FakeHermes(),
            stt_adapter=_FakeSttAdapter(),
            call_cap_seconds=0.0,
        )
        await loop.start()
        await asyncio.gather(
            loop.handle_run_event(
                _event(
                    EventKind.TOOL_STARTED,
                    1,
                    payload={"tool": "terminal"},
                )
            ),
            loop._speak_text("On it."),
        )
        await loop.wait_idle()

        assert state["max_active"] == 1
        assert sorted(state["texts"]) == ["On it.", "Running a command."]
        events = state["events"]
        assert [kind for kind, _ in events] == ["enter", "exit", "enter", "exit"]
        assert {text for kind, text in events if kind == "enter"} == {
            "On it.",
            "Running a command.",
        }
        await loop.close("test")

    asyncio.run(run())


def test_close_emits_session_closed() -> None:
    async def run() -> None:
        loop, messages, _, _, _ = _build_loop(
            decision=_decision("answer_direct", "OK."),
        )
        await loop.start()
        await loop.close("client_delete")

        assert loop.closed
        closed = _find(messages, "session-closed")
        assert closed
        assert closed[-1]["data"] == {"reason": "client_delete"}

    asyncio.run(run())


def test_router_failure_speaks_a_fallback() -> None:
    class _FailingRouter:
        async def decide(self, utterance: str) -> RouterDecision:
            raise RuntimeError("router down")

    async def run() -> None:
        messages: list[dict[str, Any]] = []
        texts: list[str] = []
        loop = CallLoop(
            _settings(),
            emit_message=messages.append,
            emit_audio=lambda chunk: None,
            tts_factory=lambda: _FakeTts(texts, []),
            router=_FailingRouter(),
            hermes=_FakeHermes(),
            stt_adapter=_FakeSttAdapter(),
            call_cap_seconds=0.0,
        )
        await loop.start()
        await loop.handle_transcript(_transcript("hello"))
        await loop.wait_idle()

        assert texts == ["Sorry, I couldn't reach the voice brain."]
        await loop.close("test")

    asyncio.run(run())


def test_invalid_dependencies_are_rejected() -> None:
    with pytest.raises(ValueError, match="emit_message"):
        CallLoop(
            _settings(),
            emit_message=None,  # type: ignore[arg-type]
            emit_audio=lambda chunk: None,
            tts_factory=lambda: _FakeTts([], []),
            router=_FakeRouter(_decision("answer_direct", "OK.")),
            hermes=_FakeHermes(),
        )
    with pytest.raises(ValueError, match="router"):
        CallLoop(
            _settings(),
            emit_message=lambda message: None,
            emit_audio=lambda chunk: None,
            tts_factory=lambda: _FakeTts([], []),
            router=object(),
            hermes=_FakeHermes(),
        )
    with pytest.raises(ValueError, match="hermes"):
        CallLoop(
            _settings(),
            emit_message=lambda message: None,
            emit_audio=lambda chunk: None,
            tts_factory=lambda: _FakeTts([], []),
            router=_FakeRouter(_decision("answer_direct", "OK.")),
            hermes=None,
        )
