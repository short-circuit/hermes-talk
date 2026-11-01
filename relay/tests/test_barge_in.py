"""Offline tests for turn detection, barge-in, and interruption.

Everything runs in-process with a fake monotonic clock, fake playback, and fake
run stopper; no sockets, models, or sleeps are used.
"""

from __future__ import annotations

import asyncio
from typing import Any

import numpy as np
import pytest
from relay.config import Settings
from relay.limits import BargeInGate
from relay.speech.audio import PcmChunk
from relay.speech.stt import TranscriptEvent
from relay.transport import (
    EndOfTurnConfig,
    EndOfTurnDetector,
    EnergyVad,
    HttpRunStopper,
    RunStopResult,
    TurnConfigError,
    TurnController,
    TurnLedger,
    TurnState,
    TurnStateError,
    VadConfig,
    VadState,
)

_SAMPLE_RATE = 16000
_FRAME_MS = 20


class FakeClock:
    """Deterministic monotonic clock for turn tests."""

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


class FakeStopper:
    """Run stopper that records run ids and returns a canned result."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.error = error

    async def stop(self, run_id: str) -> RunStopResult:
        self.calls.append(run_id)
        if self.error is not None:
            raise self.error
        return RunStopResult(
            ok=True,
            run_id=run_id,
            status="stopping",
            latency_ms=5.0,
        )


def _chunk(amplitude: int, duration_ms: int = _FRAME_MS) -> PcmChunk:
    samples = np.full(int(_SAMPLE_RATE * duration_ms / 1000), amplitude, dtype=np.int16)
    return PcmChunk(data=samples.tobytes(), sample_rate=_SAMPLE_RATE)


def _silence(duration_ms: int = _FRAME_MS) -> PcmChunk:
    return _chunk(0, duration_ms)


def _controller(
    *,
    clock: FakeClock | None = None,
    playback: FakePlayback | None = None,
    stopper: FakeStopper | None = None,
    min_speech_ms: int = 300,
    cooldown_ms: int = 1000,
) -> TurnController:
    fake_clock = clock if clock is not None else FakeClock()
    return TurnController(
        vad=EnergyVad(VadConfig(), clock=fake_clock),
        detector=EndOfTurnDetector(),
        gate=BargeInGate(
            min_speech_ms=min_speech_ms,
            cooldown_ms=cooldown_ms,
            clock=fake_clock,
        ),
        playback=playback,
        stopper=stopper,
        clock=fake_clock,
    )


def _speaking_turn(controller: TurnController, *, run_id: str = "run-1") -> None:
    controller.begin_user_turn()
    controller.commit_utterance("what time is it")
    controller.begin_reply(run_id=run_id)


async def _feed(
    controller: TurnController,
    clock: FakeClock,
    chunk_factory: Any,
    frames: int,
) -> list[Any]:
    outcomes = []
    for _ in range(frames):
        clock.advance(_FRAME_MS / 1000)
        outcomes.append(await controller.feed_mic(chunk_factory()))
    return outcomes


def _events(controller: TurnController) -> list[Any]:
    return list(controller.ledger.events())


def test_flush_happens_on_onset_and_run_is_stopped() -> None:
    clock = FakeClock()
    playback = FakePlayback()
    stopper = FakeStopper()
    controller = _controller(clock=clock, playback=playback, stopper=stopper)
    _speaking_turn(controller)

    outcomes = asyncio.run(_feed(controller, clock, lambda: _chunk(16000), 15))

    flushed = [outcome for outcome in outcomes if outcome.barge_in and outcome.barge_in.flushed]
    assert flushed
    assert flushed[0].barge_in.flush_latency_ms <= 300.0
    assert flushed[0].barge_in.within_budget is True
    assert playback.flushes == 1
    assert stopper.calls == ["run-1"]
    assert any(event.kind == "barge_in.flushed" for event in _events(controller))
    assert controller.snapshot()["turn_state"] == TurnState.INTERRUPTED.value


def test_next_turn_carries_the_interrupted_marker_once() -> None:
    clock = FakeClock()
    stopper = FakeStopper()
    controller = _controller(clock=clock, playback=FakePlayback(), stopper=stopper)
    _speaking_turn(controller)
    controller.note_spoken("The answer is forty two")

    asyncio.run(_feed(controller, clock, lambda: _chunk(16000), 15))

    marker = controller.take_marker()
    assert marker is not None
    assert "interrupted" in marker
    assert "forty two" in marker
    assert controller.take_marker() is None

    clock.advance(1.0)
    _speaking_turn(controller)
    controller.note_spoken("The answer is forty two")
    asyncio.run(_feed(controller, clock, lambda: _chunk(16000), 15))

    parts = [{"type": "text", "text": "what time is it"}]
    first = controller.with_interruption_marker(parts)
    assert len(first) == 2
    assert first[0]["type"] == "text"
    assert "interrupted" in str(first[0]["text"])
    assert "forty two" in str(first[0]["text"])
    assert parts == [{"type": "text", "text": "what time is it"}]

    second = controller.with_interruption_marker(parts)
    assert len(second) == 1
    assert second[0] == {"type": "text", "text": "what time is it"}
    assert second is not parts


def test_marker_tail_is_bounded_and_marked_when_truncated() -> None:
    clock = FakeClock()
    controller = _controller(clock=clock, playback=FakePlayback(), stopper=FakeStopper())
    _speaking_turn(controller)
    controller.note_spoken(
        "This is a deliberately long sentence that should be truncated in the marker tail for safety"
    )

    asyncio.run(_feed(controller, clock, lambda: _chunk(16000), 15))

    marker = controller.take_marker()
    assert marker is not None
    quoted = marker.split('while saying "', 1)[1].split('"', 1)[0]
    assert quoted.startswith("…")
    assert len(quoted) <= 81
    assert quoted.split()[-1] == "safety"


def test_false_trigger_flushes_but_does_not_kill_the_call() -> None:
    clock = FakeClock()
    playback = FakePlayback()
    stopper = FakeStopper()
    controller = _controller(clock=clock, playback=playback, stopper=stopper)
    _speaking_turn(controller)

    speech = asyncio.run(_feed(controller, clock, lambda: _chunk(16000), 5))
    silence = asyncio.run(_feed(controller, clock, _silence, 15))

    assert playback.flushes == 1
    assert stopper.calls == []
    assert any(
        outcome.barge_in and outcome.barge_in.suppressed_reason == "too_short"
        for outcome in speech + silence
    )
    assert any(event.kind == "barge_in.suppressed" for event in _events(controller))
    snapshot = controller.snapshot()
    assert snapshot["turn_state"] == TurnState.SPEAKING.value
    assert snapshot["session_alive"] is True
    assert controller.take_marker() is None

    completed = controller.end_reply()
    assert completed.state is TurnState.COMPLETED


def test_failing_stopper_cannot_kill_the_call() -> None:
    clock = FakeClock()
    stopper = FakeStopper(error=RuntimeError("boom"))
    controller = _controller(clock=clock, playback=FakePlayback(), stopper=stopper)

    _speaking_turn(controller)
    outcomes = asyncio.run(_feed(controller, clock, lambda: _chunk(16000), 15))

    confirmed = [
        outcome for outcome in outcomes if outcome.barge_in and outcome.barge_in.stop_accepted
    ]
    assert confirmed
    run_stop = confirmed[-1].barge_in.run_stop
    assert run_stop is not None
    assert run_stop.ok is False
    assert run_stop.error == "RuntimeError"
    snapshot = controller.snapshot()
    assert snapshot["turn_state"] == TurnState.INTERRUPTED.value
    assert snapshot["session_alive"] is True


def test_no_barge_in_while_listening() -> None:
    clock = FakeClock()
    playback = FakePlayback()
    stopper = FakeStopper()
    controller = _controller(clock=clock, playback=playback, stopper=stopper)
    controller.begin_user_turn()

    outcomes = asyncio.run(_feed(controller, clock, lambda: _chunk(16000), 15))

    assert playback.flushes == 0
    assert stopper.calls == []
    assert all(outcome.barge_in is None for outcome in outcomes)
    assert controller.snapshot()["turn_state"] == TurnState.LISTENING.value


def test_vad_classifies_silence_and_speech() -> None:
    vad = EnergyVad(VadConfig(), clock=FakeClock())

    silent = vad.process(_silence())[0]
    loud = vad.process(_chunk(16000))[0]

    assert silent.is_voiced is False
    assert silent.state is VadState.SILENCE
    assert loud.is_voiced is True


def test_vad_single_blip_does_not_onset() -> None:
    vad = EnergyVad(VadConfig(), clock=FakeClock())

    blip = vad.process(_chunk(16000))[0]
    after = vad.process(_silence())[0]

    assert blip.onset is False
    assert after.onset is False
    assert after.state is VadState.SILENCE


def test_vad_onsets_after_the_configured_window() -> None:
    vad = EnergyVad(VadConfig(), clock=FakeClock())

    frames = [vad.process(_chunk(16000))[0] for _ in range(3)]

    assert frames[0].onset is False
    assert frames[1].onset is False
    assert frames[2].onset is True
    assert frames[2].state is VadState.SPEECH
    assert frames[2].speech_ms == 60


def test_vad_offsets_after_hangover() -> None:
    vad = EnergyVad(VadConfig(), clock=FakeClock())
    for _ in range(3):
        vad.process(_chunk(16000))

    frames = [vad.process(_silence())[0] for _ in range(15)]

    assert frames[-1].offset is True
    assert frames[-1].state is VadState.SILENCE
    assert vad.snapshot()["offsets"] == 1


def test_vad_noise_floor_adapts_to_a_steady_hum() -> None:
    vad = EnergyVad(VadConfig(), clock=FakeClock())
    final = None
    for _ in range(600):
        final = vad.process(_chunk(8000))[0]

    assert final is not None
    assert final.is_voiced is False
    assert vad.snapshot()["noise_floor_db"] > -40.0


def test_vad_snapshot_has_no_audio_bytes() -> None:
    vad = EnergyVad(VadConfig(), clock=FakeClock())
    vad.process(_chunk(16000))
    snapshot = vad.snapshot()

    assert "data" not in snapshot
    assert all(not isinstance(value, (bytes, bytearray)) for value in snapshot.values())


def test_vad_rejects_wrong_sample_rate() -> None:
    vad = EnergyVad(VadConfig(), clock=FakeClock())
    chunk = PcmChunk(data=bytes(320), sample_rate=8000)

    with pytest.raises(ValueError, match="16000"):
        vad.process(chunk)


def test_vad_rejects_oversized_chunks() -> None:
    vad = EnergyVad(VadConfig(), clock=FakeClock())
    chunk = _chunk(16000, duration_ms=600)

    with pytest.raises(ValueError, match="500"):
        vad.process(chunk)


def test_semantic_end_of_turn_commits_on_punctuation_and_silence() -> None:
    detector = EndOfTurnDetector()

    decision = detector.observe(
        text="What time is it?",
        is_final=True,
        endpoint=False,
        speech_ms=1000,
        silence_ms=250,
    )

    assert decision.action == "commit"
    assert decision.reason == "punctuation_and_silence"


def test_semantic_end_of_turn_holds_on_trailing_filler() -> None:
    detector = EndOfTurnDetector()

    decision = detector.observe(
        text="I was going to",
        is_final=False,
        endpoint=False,
        speech_ms=1000,
        silence_ms=5000,
    )

    assert decision.action == "hold"
    assert decision.reason == "trailing_filler"


def test_semantic_end_of_turn_commits_on_long_silence() -> None:
    detector = EndOfTurnDetector()

    decision = detector.observe(
        text="what time is it",
        is_final=True,
        endpoint=False,
        speech_ms=1000,
        silence_ms=600,
    )

    assert decision.action == "commit"
    assert decision.reason == "trailing_silence"


def test_semantic_end_of_turn_continues_while_speech_is_active() -> None:
    detector = EndOfTurnDetector()

    decision = detector.observe(
        text="what time is it",
        is_final=False,
        endpoint=False,
        speech_ms=1000,
        silence_ms=100,
    )

    assert decision.action == "continue"
    assert decision.reason == "awaiting_more"


def test_semantic_end_of_turn_forces_commit_at_max_utterance() -> None:
    detector = EndOfTurnDetector()

    decision = detector.observe(
        text="this story keeps going",
        is_final=False,
        endpoint=False,
        speech_ms=30_000,
        silence_ms=0,
    )

    assert decision.action == "commit"
    assert decision.reason == "max_utterance"


def test_semantic_end_of_turn_discards_empty_text() -> None:
    detector = EndOfTurnDetector()

    discarded = detector.observe(
        text="   ",
        is_final=True,
        endpoint=False,
        speech_ms=0,
        silence_ms=0,
    )
    continued = detector.observe(
        text="",
        is_final=True,
        endpoint=False,
        speech_ms=100,
        silence_ms=0,
    )

    assert discarded.action == "discard"
    assert continued.action == "continue"


def test_semantic_end_of_turn_accepts_non_string_text() -> None:
    detector = EndOfTurnDetector()

    decision = detector.observe(
        text=123,  # type: ignore[arg-type]
        is_final=False,
        endpoint=False,
        speech_ms=0,
        silence_ms=0,
    )

    assert decision.text == "123"
    assert decision.action == "continue"


class FakeResponse:
    """Minimal httpx response stand-in."""

    def __init__(self, status_code: int, body: object = None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body


class FakeHttpClient:
    """Minimal async httpx client stand-in."""

    def __init__(
        self,
        *,
        response: FakeResponse | None = None,
        error: Exception | None = None,
        clock: FakeClock | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.clock = clock
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def post(self, url: str, *, headers: dict[str, str]) -> FakeResponse:
        self.calls.append((url, headers))
        if self.clock is not None:
            self.clock.advance(0.005)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def test_http_run_stopper_success() -> None:
    clock = FakeClock()
    client = FakeHttpClient(
        response=FakeResponse(200, {"status": "stopping"}),
        clock=clock,
    )
    stopper = HttpRunStopper(
        "http://127.0.0.1:8642",
        "top-secret",
        client=client,
        clock=clock,
    )

    result = asyncio.run(stopper.stop("run-1"))

    assert result.ok is True
    assert result.status == "stopping"
    assert result.latency_ms > 0.0
    assert client.calls == [
        (
            "http://127.0.0.1:8642/v1/runs/run-1/stop",
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer top-secret",
            },
        )
    ]


def test_http_run_stopper_treats_run_not_active_as_non_fatal() -> None:
    client = FakeHttpClient(
        response=FakeResponse(409, {"error": "run_not_active", "status": "cancelled"})
    )
    stopper = HttpRunStopper("http://127.0.0.1:8642", client=client)

    result = asyncio.run(stopper.stop("run-1"))

    assert result.ok is False
    assert result.status == "cancelled"
    assert result.error == "run_not_active"


def test_http_run_stopper_reports_other_http_errors() -> None:
    client = FakeHttpClient(response=FakeResponse(500, {"status": "error"}))
    stopper = HttpRunStopper("http://127.0.0.1:8642", client=client)

    result = asyncio.run(stopper.stop("run-1"))

    assert result.ok is False
    assert "500" in str(result.error)


def test_http_run_stopper_never_raises_or_leaks_the_key() -> None:
    client = FakeHttpClient(error=RuntimeError("socket closed"))
    stopper = HttpRunStopper(
        "http://127.0.0.1:8642",
        "top-secret",
        client=client,
    )

    result = asyncio.run(stopper.stop("run-1"))

    assert result.ok is False
    assert result.error == "RuntimeError"
    assert "top-secret" not in str(result.error)


def test_ledger_rejects_illegal_transitions_and_snapshots_counters() -> None:
    clock = FakeClock()
    ledger = TurnLedger(clock=clock)
    record = ledger.start()
    assert record.state is TurnState.LISTENING

    with pytest.raises(TurnStateError):
        ledger.transition(record, TurnState.SPEAKING)

    ledger.transition(record, TurnState.COMMITTED)
    with pytest.raises(TurnStateError):
        ledger.transition(record, TurnState.SPEAKING)

    ledger.transition(record, TurnState.THINKING)
    ledger.transition(record, TurnState.SPEAKING)
    ledger.mark_interrupted(
        record,
        reason="barge_in",
        speech_ms=300,
        flush_latency_ms=20.0,
        run_id="run-1",
    )
    ledger.mark_completed(record)

    snapshot = ledger.snapshot()
    assert snapshot["max_events"] == 200
    assert all(isinstance(value, int) for value in snapshot.values())
    assert not any(isinstance(value, (bytes, bytearray)) for value in snapshot.values())
    assert any(event.kind == "turn.interrupted" for event in ledger.events())
    assert any(event.kind == "turn.completed" for event in ledger.events())


def test_ledger_history_is_bounded() -> None:
    ledger = TurnLedger(clock=FakeClock())
    for _ in range(205):
        ledger.start()

    assert len(ledger.events()) == 200
    assert ledger.snapshot()["events"] == 200


def test_from_settings_builds_a_controller_without_network() -> None:
    settings = Settings(
        _env_file=None,
        barge_in_min_speech_ms=250,
        barge_in_cooldown_ms=1500,
        hermes_base_url="http://127.0.0.1:9999",
        hermes_api_key="top-secret",
    )

    controller = TurnController.from_settings(settings)

    assert isinstance(controller, TurnController)
    assert isinstance(controller._vad, EnergyVad)
    assert controller._gate._min_speech_ms == 250
    assert controller._gate._cooldown_seconds == 1.5
    assert isinstance(controller._stopper, HttpRunStopper)
    assert controller._stopper._base_url == "http://127.0.0.1:9999"
    assert controller._stopper._api_key == "top-secret"
    assert controller._stopper._client is None
    assert controller.snapshot()["session_alive"] is True


def test_from_settings_honours_injected_ports() -> None:
    settings = Settings(_env_file=None)
    playback = FakePlayback()
    stopper = FakeStopper()

    controller = TurnController.from_settings(settings, playback=playback, stopper=stopper)

    assert controller._playback is playback
    assert controller._stopper is stopper


def test_controller_validates_constructor_arguments() -> None:
    with pytest.raises(TurnConfigError):
        TurnController(
            vad=EnergyVad(VadConfig()),
            detector=EndOfTurnDetector(),
            gate=BargeInGate(min_speech_ms=300, cooldown_ms=1000, clock=FakeClock()),
            clock=FakeClock(),
            flush_budget_ms=0,
        )


def test_transcript_event_feeds_the_detector() -> None:
    controller = _controller(clock=FakeClock())
    event = TranscriptEvent(
        kind="final",
        text="What time is it?",
        engine="fake",
        latency_ms=1.0,
        audio_ms=1000.0,
        is_endpoint=True,
    )

    decision = controller.on_transcript(event)

    assert decision.action == "commit"
    assert decision.reason == "stt_endpoint"


def test_explicit_stop_reply_interrupts_thinking_turn() -> None:
    clock = FakeClock()
    stopper = FakeStopper()
    controller = _controller(clock=clock, stopper=stopper)
    controller.begin_user_turn()
    controller.commit_utterance("what time is it")
    controller.begin_reply()

    interruption = asyncio.run(controller.stop_reply(reason="user_barge_in", speech_ms=120))

    assert interruption.reason == "user_barge_in"
    assert interruption.run_stop is None
    assert "interrupted" in interruption.marker
    assert controller.snapshot()["turn_state"] == TurnState.INTERRUPTED.value


def test_end_of_turn_config_validates_values() -> None:
    with pytest.raises(TurnConfigError):
        EndOfTurnConfig(silence_commit_ms=-1)
    with pytest.raises(TurnConfigError):
        VadConfig(frame_ms=0)
