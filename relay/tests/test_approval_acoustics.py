"""Offline tests for playback/echo and ambient-speech approval hardening.

Every test uses injected ``now=`` values, recording sinks, and ``tmp_path``.
No test sleeps, opens a network connection, or touches real audio.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest
from relay.brain.approvals import (
    ApprovalGate,
    ApprovalRequest,
    Decision,
    Provenance,
    Utterance,
)
from relay.config import Settings
from relay.logging import configure_audit, reset_audit


def _request(
    *,
    prompt_id: str = "prompt-1",
    execution_generation: int = 1,
    command: str | None = "rm -rf build",
    timeout_s: float = 20.0,
) -> ApprovalRequest:
    return ApprovalRequest(
        prompt_id=prompt_id,
        execution_generation=execution_generation,
        tool="terminal",
        action_summary="Delete the build directory",
        required_phrase="yes run it",
        timeout_s=timeout_s,
        command=command,
        description="delete in root path",
        run_id="run-1",
    )


def _recording_sink() -> tuple[list[Decision], Callable[[Decision], None]]:
    posted: list[Decision] = []

    def record(decision: Decision) -> None:
        posted.append(decision)

    return posted, record


def test_playback_frames_ignored_then_post_playback_utterance_approves() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request()
    assert gate.request(request, now=10.0) == request
    gate.playback_started(now=10.0)

    ignored = gate.answer(
        Utterance(
            text="yes run it",
            provenance=Provenance.SPOKEN,
            onset_at=10.5,
            from_playback_frames=True,
        ),
        now=10.6,
    )
    assert ignored is None
    assert posted == []
    assert gate.pending == request
    assert gate.ignored_count == 1
    assert gate.last_ignored_reason == "playback_frames"

    gate.playback_finished(now=12.0)
    decision = gate.answer(
        Utterance(text="yes run it", provenance=Provenance.SPOKEN, onset_at=12.0),
        now=12.1,
    )
    assert decision is not None
    assert decision.approved is True
    assert posted == [decision]


def test_onset_before_playback_close_is_ignored_and_equal_onset_approves() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request()
    assert gate.request(request, now=10.0) == request
    gate.playback_started(now=10.0)
    gate.playback_finished(now=12.0)

    ignored = gate.answer(
        Utterance(text="yes run it", provenance=Provenance.SPOKEN, onset_at=11.5),
        now=12.1,
    )
    assert ignored is None
    assert gate.ignored_count == 1
    assert gate.last_ignored_reason == "onset_before_playback_end"
    assert gate.pending == request

    decision = gate.answer(
        Utterance(text="yes run it", provenance=Provenance.SPOKEN, onset_at=12.0),
        now=12.2,
    )
    assert decision is not None
    assert decision.approved is True
    assert posted == [decision]


def test_active_playback_ignores_utterance_until_playback_finishes() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request()
    assert gate.request(request, now=10.0) == request
    gate.playback_started(now=10.0)
    utterance = Utterance(text="yes run it", provenance=Provenance.SPOKEN, onset_at=10.5)

    ignored = gate.answer(utterance, now=10.5)
    assert ignored is None
    assert gate.ignored_count == 1
    assert gate.last_ignored_reason == "playback_active"
    assert gate.pending == request

    gate.playback_finished(now=10.5)
    decision = gate.answer(utterance, now=10.6)
    assert decision is not None
    assert decision.approved is True
    assert posted == [decision]


def test_barge_in_cut_at_speech_onset_allows_same_onset_utterance() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request()
    assert gate.request(request, now=9.0) == request
    gate.playback_started(now=9.0)
    gate.barge_in(now=10.0, onset_at=9.5)

    assert gate.playback_closed_at == 9.5
    decision = gate.answer(
        Utterance(text="yes run it", provenance=Provenance.SPOKEN, onset_at=9.5),
        now=10.1,
    )
    assert decision is not None
    assert decision.approved is True
    assert posted == [decision]


def test_recorded_playback_without_onset_is_ignored() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request()
    assert gate.request(request, now=10.0) == request
    gate.playback_started(now=10.0)
    gate.playback_finished(now=12.0)

    ignored = gate.answer(
        Utterance(text="yes run it", provenance=Provenance.SPOKEN),
        now=12.1,
    )
    assert ignored is None
    assert gate.ignored_count == 1
    assert gate.last_ignored_reason == "onset_unknown"
    assert gate.pending == request
    assert posted == []


def test_ambient_bare_yes_denies_and_a_named_action_approves() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request()
    assert gate.request(request, now=10.0) == request

    denied = gate.answer(Utterance(text="yes", provenance=Provenance.SPOKEN), now=11.0)
    assert denied is not None
    assert denied.approved is False
    assert denied.reason == "phrase_mismatch"
    assert posted == [denied]

    assert (
        gate.answer(Utterance(text="yes, run it", provenance=Provenance.SPOKEN), now=12.0) is None
    )
    assert posted == [denied]

    approved_posted, approved_sink = _recording_sink()
    approved_gate = ApprovalGate(audit=False, on_decide=approved_sink)
    approved_request = _request(prompt_id="prompt-2", execution_generation=2)
    assert approved_gate.request(approved_request, now=20.0) == approved_request
    approved = approved_gate.answer(
        Utterance(text="yes, run it", provenance=Provenance.SPOKEN),
        now=21.0,
    )
    assert approved is not None
    assert approved.approved is True
    assert approved_posted == [approved]


def test_overlapping_speakers_deny_and_do_not_reopen() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request()
    assert gate.request(request, now=10.0) == request

    denied = gate.answer(
        Utterance(text="yes run it", provenance=Provenance.SPOKEN, speaker_count=2),
        now=11.0,
    )
    assert denied is not None
    assert denied.approved is False
    assert denied.reason == "overlapping_speakers"
    assert posted == [denied]

    assert gate.answer(Utterance(text="yes run it", provenance=Provenance.SPOKEN), now=12.0) is None
    assert posted == [denied]


def test_second_yes_with_no_pending_prompt_is_ignored() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    assert gate.answer(Utterance(text="yes run it", provenance=Provenance.SPOKEN), now=1.0) is None
    assert posted == []


def test_configurable_confirmation_phrase_accepts_only_the_named_action() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(
        audit=False,
        on_decide=sink,
        confirmation_phrase="yes delete it",
    )
    request = _request()
    assert gate.request(request, now=10.0) == request
    assert "yes delete it" in gate.readout(now=10.1).text

    approved = gate.answer(
        Utterance(text="yes delete it", provenance=Provenance.SPOKEN),
        now=11.0,
    )
    assert approved is not None
    assert approved.approved is True
    assert posted == [approved]

    rejected_posted, rejected_sink = _recording_sink()
    rejected_gate = ApprovalGate(
        audit=False,
        on_decide=rejected_sink,
        confirmation_phrase="yes delete it",
    )
    rejected_request = _request(prompt_id="prompt-2", execution_generation=2)
    assert rejected_gate.request(rejected_request, now=20.0) == rejected_request
    rejected = rejected_gate.answer(
        Utterance(text="yes run it", provenance=Provenance.SPOKEN),
        now=21.0,
    )
    assert rejected is not None
    assert rejected.approved is False
    assert rejected.reason == "phrase_mismatch"
    assert rejected_posted == [rejected]

    with pytest.raises(ValueError):
        ApprovalGate(confirmation_phrase="yes")


def test_confidence_floor_is_configurable() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, min_confidence=0.9, on_decide=sink)
    request = _request()
    assert gate.request(request, now=10.0) == request
    denied = gate.answer(
        Utterance(text="yes run it", provenance=Provenance.SPOKEN, confidence=0.8),
        now=11.0,
    )
    assert denied is not None
    assert denied.approved is False
    assert denied.reason == "low_confidence"
    assert posted == [denied]

    accepted_posted, accepted_sink = _recording_sink()
    accepted_gate = ApprovalGate(audit=False, min_confidence=0.9, on_decide=accepted_sink)
    accepted_request = _request(prompt_id="prompt-2", execution_generation=2)
    assert accepted_gate.request(accepted_request, now=20.0) == accepted_request
    approved = accepted_gate.answer(
        Utterance(text="yes run it", provenance=Provenance.SPOKEN, confidence=0.95),
        now=21.0,
    )
    assert approved is not None
    assert approved.approved is True
    assert accepted_posted == [approved]


def test_echo_guard_disabled_allows_playback_frames_to_approve() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, echo_guard=False, on_decide=sink)
    request = _request()
    assert gate.request(request, now=10.0) == request
    gate.playback_started(now=10.0)

    approved = gate.answer(
        Utterance(
            text="yes run it",
            provenance=Provenance.SPOKEN,
            onset_at=10.5,
            from_playback_frames=True,
        ),
        now=10.6,
    )
    assert approved is not None
    assert approved.approved is True
    assert gate.ignored_count == 0
    assert posted == [approved]


def test_audit_records_ignored_reason_and_decided_transcript_hash(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    sink = configure_audit(settings)
    assert sink is not None
    try:
        gate = ApprovalGate(audit=True)
        request = _request()
        assert gate.request(request, now=10.0) == request
        gate.playback_started(now=10.0)
        assert (
            gate.answer(
                Utterance(
                    text="yes run it",
                    provenance=Provenance.SPOKEN,
                    onset_at=10.5,
                    from_playback_frames=True,
                ),
                now=10.6,
            )
            is None
        )
        gate.playback_finished(now=12.0)
        denied = gate.answer(
            Utterance(text="no", provenance=Provenance.SPOKEN, onset_at=12.0),
            now=12.1,
        )
        assert denied is not None
        assert denied.reason == "spoken_negation"

        entries = sink.read_entries()
        ignored = next(entry for entry in entries if entry["event"] == "approval.ignored")
        decided = next(entry for entry in entries if entry["event"] == "approval.decided")
        assert ignored["prompt_id"] == request.prompt_id
        assert ignored["execution_generation"] == request.execution_generation
        assert ignored["reason"] == "playback_frames"
        assert ignored["transcript_sha256"] == hashlib.sha256(b"yes run it").hexdigest()
        assert decided["prompt_id"] == request.prompt_id
        assert decided["execution_generation"] == request.execution_generation
        assert decided["decision"] == "denied"
        assert decided["reason"] == "spoken_negation"
        assert decided["transcript_sha256"] == hashlib.sha256(b"no").hexdigest()
    finally:
        reset_audit()
