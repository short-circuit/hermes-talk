"""Offline tests for tool-activity narration and its redaction boundary."""

from __future__ import annotations

from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path

import pytest
from relay.brain.narration import (
    DEFAULT_TOOL_PHRASE,
    MAX_NARRATION_CHARS,
    MIN_NARRATION_INTERVAL_S,
    TOOL_PHRASES,
    ToolNarrator,
    narrate_tool_completed,
    redact_spoken,
)
from relay.hermes import EventKind, RunEvent
from relay.logging import register_secret, reset_registered_secrets

_SECRET = "sk-" + "test-0123456789abcdefghij"


class FakeClock:
    """Deterministic clock backed by an explicit sequence of readings."""

    def __init__(self, times: Iterator[float]) -> None:
        self._times = times

    def __call__(self) -> float:
        return next(self._times)


def _event(
    kind: EventKind,
    *,
    run_id: str = "run-a",
    seq: int = 0,
    timestamp: float = 0.0,
    **payload: object,
) -> RunEvent:
    return RunEvent(
        kind=kind,
        raw_name=kind.value,
        seq=seq,
        run_id=run_id,
        timestamp=timestamp,
        payload=payload,
    )


def test_first_tool_start_narrates_immediately() -> None:
    clock = FakeClock(iter([0.0]))
    narrator = ToolNarrator(clock=clock)
    event = _event(EventKind.TOOL_STARTED, timestamp=0.0, tool="read_file")

    update = narrator.observe(event)

    assert update is not None
    assert update.emit_at - event.timestamp <= 1.0
    assert update.latency_s == 0.0
    assert update.text == TOOL_PHRASES["read_file"]
    assert update.tool == "read_file"
    assert update.suppressed_since_last == 0


def test_throttle_allows_one_phrase_per_interval() -> None:
    times = [0.0, 0.5, 3.999, 4.0, 7.9]
    clock = FakeClock(iter(times))
    narrator = ToolNarrator(clock=clock)
    updates = []

    for seq, timestamp in enumerate(times):
        update = narrator.observe(
            _event(EventKind.TOOL_STARTED, seq=seq, timestamp=timestamp, tool="terminal")
        )
        if update is not None:
            updates.append(update)

    assert [update.emit_at for update in updates] == [0.0, 4.0]
    assert [update.suppressed_since_last for update in updates] == [0, 2]
    assert all(
        second.emit_at - first.emit_at >= MIN_NARRATION_INTERVAL_S
        for first, second in pairwise(updates)
    )
    snapshot = narrator.snapshot()
    assert snapshot.emitted == 2
    assert snapshot.suppressed == 3
    assert snapshot.last_emit_at == 4.0


def test_fast_tool_chain_is_suppressed() -> None:
    times = [index * 0.05 for index in range(12)]
    clock = FakeClock(iter(times))
    narrator = ToolNarrator(clock=clock)
    updates = []

    for seq, timestamp in enumerate(times):
        update = narrator.observe(
            _event(EventKind.TOOL_STARTED, seq=seq, timestamp=timestamp, tool="terminal")
        )
        if update is not None:
            updates.append(update)
        completed = narrator.observe(
            _event(
                EventKind.TOOL_COMPLETED,
                seq=100 + seq,
                timestamp=timestamp + 0.01,
                tool="terminal",
                output="noise",
            )
        )
        assert completed is None

    assert len(updates) == 1
    snapshot = narrator.snapshot()
    assert snapshot.emitted == 1
    assert snapshot.suppressed == 11


def test_tool_narrator_never_reads_raw_output() -> None:
    raw_output = "RAW-OUTPUT-" + "x" * 500
    clock = FakeClock(iter([0.0]))
    narrator = ToolNarrator(clock=clock)

    update = narrator.observe(
        _event(
            EventKind.TOOL_STARTED,
            timestamp=0.0,
            tool="terminal",
            output=raw_output,
        )
    )
    assert update is not None
    assert raw_output not in update.text
    assert update.text == TOOL_PHRASES["terminal"]

    completed = narrator.observe(
        _event(
            EventKind.TOOL_COMPLETED,
            timestamp=0.1,
            tool="terminal",
            output=raw_output,
        )
    )
    assert completed is None
    assert narrator.snapshot().last_text == TOOL_PHRASES["terminal"]


def test_unknown_tool_uses_default_phrase_without_echoing_name() -> None:
    raw_tool = "quantum_flux_capacitor"
    clock = FakeClock(iter([0.0]))
    narrator = ToolNarrator(clock=clock)

    update = narrator.observe(_event(EventKind.TOOL_STARTED, timestamp=0.0, tool=raw_tool))

    assert update is not None
    assert update.text == DEFAULT_TOOL_PHRASE
    assert raw_tool not in update.text


def test_redaction_applies_to_emitted_and_legacy_text() -> None:
    register_secret(_SECRET)
    home = Path("~").expanduser()
    clock = FakeClock(iter([0.0]))
    narrator = ToolNarrator(
        clock=clock,
        phrases={"terminal": f"Reading {_SECRET} at {home}/notes.txt"},
    )

    update = narrator.observe(_event(EventKind.TOOL_STARTED, timestamp=0.0, tool="terminal"))
    assert update is not None
    assert "***" in update.text
    assert "~" in update.text
    assert _SECRET not in update.text
    assert str(home) not in update.text
    assert "/home/" not in update.text

    completed = narrate_tool_completed("terminal", f"Found {_SECRET} in {home}/notes.txt")
    assert _SECRET not in completed
    assert str(home) not in completed
    assert "/home/" not in completed
    reset_registered_secrets()


def test_reset_allows_immediate_emission() -> None:
    clock = FakeClock(iter([0.0, 0.1, 0.2]))
    narrator = ToolNarrator(clock=clock)

    first = narrator.observe(_event(EventKind.TOOL_STARTED, timestamp=0.0, tool="terminal"))
    assert first is not None
    assert narrator.observe(_event(EventKind.TOOL_STARTED, timestamp=0.1, tool="terminal")) is None

    narrator.reset()
    third = narrator.observe(_event(EventKind.TOOL_STARTED, timestamp=0.2, tool="terminal"))

    assert third is not None
    assert third.suppressed_since_last == 0
    snapshot = narrator.snapshot()
    assert snapshot.emitted == 1
    assert snapshot.suppressed == 0


@pytest.mark.parametrize(
    "kind",
    [EventKind.RUN_COMPLETED, EventKind.RUN_FAILED, EventKind.RUN_CANCELLED],
)
def test_terminal_event_rebases_throttle(kind: EventKind) -> None:
    clock = FakeClock(iter([0.0, 0.6]))
    narrator = ToolNarrator(clock=clock)

    first = narrator.observe(_event(EventKind.TOOL_STARTED, timestamp=0.0, tool="terminal"))
    assert first is not None
    assert narrator.observe(_event(kind, timestamp=0.5)) is None

    update = narrator.observe(_event(EventKind.TOOL_STARTED, timestamp=0.6, tool="terminal"))

    assert update is not None
    assert update.emit_at == 0.6


@pytest.mark.parametrize("interval", [0.0, -1.0])
def test_non_positive_interval_rejected(interval: float) -> None:
    with pytest.raises(ValueError):
        ToolNarrator(min_interval_s=interval)


def test_backwards_clock_still_emits() -> None:
    clock = FakeClock(iter([5.0, 1.0]))
    narrator = ToolNarrator(clock=clock)

    first = narrator.observe(_event(EventKind.TOOL_STARTED, timestamp=5.0, tool="terminal"))
    second = narrator.observe(_event(EventKind.TOOL_STARTED, timestamp=1.0, tool="terminal"))

    assert first is not None
    assert second is not None
    assert second.emit_at == 1.0
    snapshot = narrator.snapshot()
    assert snapshot.last_emit_at == 1.0
    assert snapshot.suppressed == 0


def test_run_id_scoping_ignores_other_runs() -> None:
    clock = FakeClock(iter([0.0]))
    narrator = ToolNarrator(clock=clock, run_id="run-a")

    ignored = narrator.observe(
        _event(EventKind.TOOL_STARTED, run_id="run-b", timestamp=0.0, tool="terminal")
    )
    update = narrator.observe(
        _event(EventKind.TOOL_STARTED, run_id="run-a", timestamp=0.0, tool="terminal")
    )

    assert ignored is None
    assert update is not None
    assert update.run_id == "run-a"
    assert narrator.snapshot().run_id == "run-a"


def test_emitted_text_is_bounded() -> None:
    long_phrase = " ".join(["word"] * 100)
    clock = FakeClock(iter([0.0, 5.0]))
    narrator = ToolNarrator(clock=clock, phrases={"terminal": long_phrase})

    first = narrator.observe(_event(EventKind.TOOL_STARTED, timestamp=0.0, tool="terminal"))
    second = narrator.observe(_event(EventKind.TOOL_STARTED, timestamp=5.0, tool="terminal"))

    assert first is not None
    assert second is not None
    assert first.text
    assert len(first.text) <= MAX_NARRATION_CHARS
    assert len(second.text) <= MAX_NARRATION_CHARS
    assert all(
        len(redact_spoken(phrase)) <= MAX_NARRATION_CHARS for phrase in TOOL_PHRASES.values()
    )


def test_empty_redaction_falls_back_to_default_phrase() -> None:
    clock = FakeClock(iter([0.0]))
    narrator = ToolNarrator(clock=clock, phrases={"terminal": "```"})

    update = narrator.observe(_event(EventKind.TOOL_STARTED, timestamp=0.0, tool="terminal"))

    assert update is not None
    assert update.text == DEFAULT_TOOL_PHRASE
