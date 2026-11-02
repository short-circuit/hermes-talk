"""Offline tests for stale-id replay and transport-failure handling.

The suite uses an injected fake clock (``now=`` values), recording sinks, and
``tmp_path`` only.  It makes no network calls and never sleeps.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from relay.brain.approvals import (
    ApprovalGate,
    ApprovalPostError,
    ApprovalRequest,
    ControlId,
    Decision,
    Provenance,
    StaleControlIdError,
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


def _utterance() -> Utterance:
    return Utterance(text="yes run it", provenance=Provenance.SPOKEN)


def test_replay_of_settled_prompt_is_idempotent_and_posts_once() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request()
    assert gate.request(request, now=10.0) == request
    decision = gate.answer(_utterance(), now=11.0)
    assert decision is not None
    assert decision.approved is True
    assert posted == [decision]

    assert gate.answer(_utterance(), control=request.control_id, now=12.0) is None
    assert posted == [decision]
    assert gate.decision_for(request.prompt_id, request.execution_generation) == decision


def test_previous_prompt_replay_reasks_then_denies_the_new_prompt() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    old_request = _request(prompt_id="prompt-old", execution_generation=1)
    assert gate.request(old_request, now=10.0) == old_request
    old_decision = gate.answer(_utterance(), now=11.0)
    assert old_decision is not None
    assert old_decision.approved is True

    new_request = _request(prompt_id="prompt-new", execution_generation=2)
    assert gate.request(new_request, now=20.0) == new_request
    with pytest.raises(StaleControlIdError) as replay_error:
        gate.answer(_utterance(), control=old_request.control_id, now=21.0)
    assert replay_error.value.reason == "prompt_mismatch"
    assert replay_error.value.expected == new_request.control_id
    assert replay_error.value.received == old_request.control_id
    assert gate.needs_reask is True
    assert gate.reask_count == 1
    assert gate.pending == new_request
    assert posted == [old_decision]

    denied = gate.answer(_utterance(), control=old_request.control_id, now=22.0)
    assert denied is not None
    assert denied.approved is False
    assert denied.reason == "reask_exhausted"
    assert denied.prompt_id == new_request.prompt_id
    assert denied.execution_generation == new_request.execution_generation
    assert posted == [old_decision, denied]
    assert gate.needs_reask is False
    assert gate.decision() == denied


def test_stale_generation_twice_reasks_then_denies() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request(prompt_id="prompt-generation", execution_generation=5)
    assert gate.request(request, now=10.0) == request
    wrong = ControlId(request.prompt_id, 6)

    with pytest.raises(StaleControlIdError) as stale_error:
        gate.answer(_utterance(), control=wrong, now=11.0)
    assert stale_error.value.reason == "stale_generation"
    assert gate.needs_reask is True
    assert gate.reask_count == 1
    assert posted == []

    denied = gate.answer(_utterance(), control=wrong, now=12.0)
    assert denied is not None
    assert denied.approved is False
    assert denied.reason == "reask_exhausted"
    assert denied.prompt_id == request.prompt_id
    assert denied.execution_generation == request.execution_generation
    assert posted == [denied]


def test_max_reasks_zero_denies_on_the_first_stale_attempt() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, max_reasks=0, on_decide=sink)
    request = _request()
    assert gate.request(request, now=10.0) == request

    denied = gate.answer(_utterance(), control=ControlId("prompt-other", 1), now=11.0)
    assert denied is not None
    assert denied.approved is False
    assert denied.reason == "reask_exhausted"
    assert denied.prompt_id == request.prompt_id
    assert denied.execution_generation == request.execution_generation
    assert posted == [denied]
    assert gate.needs_reask is False


def test_stale_409_reopens_once_then_the_same_ids_approve() -> None:
    posted: list[Decision] = []
    approve_calls = 0

    def sink(decision: Decision) -> None:
        nonlocal approve_calls
        if decision.approved:
            approve_calls += 1
            if approve_calls == 1:
                raise ApprovalPostError(
                    "approval no longer pending",
                    status=409,
                    code="approval_not_pending",
                )
        posted.append(decision)

    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request()
    assert gate.request(request, now=10.0) == request

    first = gate.answer(_utterance(), control=request.control_id, now=11.0)
    assert first is None
    assert gate.needs_reask is True
    assert gate.pending == request
    assert gate.decision() is None
    assert gate.reask_count == 1
    assert gate.post_error is not None
    assert posted == []

    second = gate.answer(_utterance(), control=request.control_id, now=12.0)
    assert second is not None
    assert second.approved is True
    assert posted == [second]
    assert len([decision for decision in posted if decision.approved]) == 1


def test_stale_409_twice_settles_stale_after_reask_and_never_approves() -> None:
    posted: list[Decision] = []
    approve_calls = 0

    def sink(decision: Decision) -> None:
        nonlocal approve_calls
        if decision.approved:
            approve_calls += 1
            if approve_calls <= 2:
                raise ApprovalPostError(
                    "approval no longer pending",
                    status=409,
                    code="approval_not_pending",
                )
        posted.append(decision)

    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request()
    assert gate.request(request, now=10.0) == request

    first = gate.answer(_utterance(), control=request.control_id, now=11.0)
    assert first is None
    assert gate.needs_reask is True
    assert gate.pending == request

    denied = gate.answer(_utterance(), control=request.control_id, now=12.0)
    assert denied is not None
    assert denied.approved is False
    assert denied.reason == "stale_after_reask"
    assert denied.prompt_id == request.prompt_id
    assert denied.execution_generation == request.execution_generation
    assert posted == [denied]
    assert not any(decision.approved for decision in posted)
    assert gate.decision() == denied


def test_non_stale_post_error_flips_approval_to_transport_deny() -> None:
    posted: list[Decision] = []

    def sink(decision: Decision) -> None:
        if decision.approved:
            raise ApprovalPostError("service unavailable", status=503)
        posted.append(decision)

    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request()
    assert gate.request(request, now=10.0) == request

    denied = gate.answer(_utterance(), control=request.control_id, now=11.0)
    assert denied is not None
    assert denied.approved is False
    assert denied.reason == "transport_error"
    assert denied.prompt_id == request.prompt_id
    assert denied.execution_generation == request.execution_generation
    assert posted == [denied]
    assert gate.decision() == denied


def test_deny_rejected_by_the_wire_keeps_its_original_reason() -> None:
    def sink(decision: Decision) -> None:
        raise ApprovalPostError(
            "approval no longer pending", status=409, code="approval_not_pending"
        )

    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request()
    assert gate.request(request, now=10.0) == request

    denied = gate.answer(
        Utterance(text="yes", provenance=Provenance.SPOKEN), control=request.control_id, now=11.0
    )
    assert denied is not None
    assert denied.approved is False
    assert denied.reason == "phrase_mismatch"
    assert gate.decision() == denied
    assert gate.post_error is not None


def test_duplicate_answer_with_settled_ids_is_ignored() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request()
    assert gate.request(request, now=10.0) == request
    decision = gate.answer(_utterance(), control=request.control_id, now=11.0)
    assert decision is not None
    assert posted == [decision]

    assert gate.answer(_utterance(), control=request.control_id, now=12.0) is None
    assert gate.answer(_utterance(), control=request.control_id, now=13.0) is None
    assert posted == [decision]


def test_audit_trail_records_reask_exhausted_stale_after_reask_and_transport_error(
    tmp_path: Path,
) -> None:
    try:
        reask_dir = tmp_path / "reask_exhausted"
        reask_sink = configure_audit(Settings(_env_file=None, data_dir=str(reask_dir)))
        assert reask_sink is not None
        reask_posted, reask_recorder = _recording_sink()
        reask_gate = ApprovalGate(audit=True, max_reasks=0, on_decide=reask_recorder)
        reask_request = _request(prompt_id="prompt-reask", execution_generation=7)
        assert reask_gate.request(reask_request, now=10.0) == reask_request
        reask_denied = reask_gate.answer(
            _utterance(),
            control=ControlId("prompt-other", 7),
            now=11.0,
        )
        assert reask_denied is not None
        assert reask_posted == [reask_denied]
        reask_decided = next(
            entry for entry in reask_sink.read_entries() if entry["event"] == "approval.decided"
        )
        assert reask_decided["prompt_id"] == reask_request.prompt_id
        assert reask_decided["execution_generation"] == reask_request.execution_generation
        assert reask_decided["decision"] == "denied"
        assert reask_decided["reason"] == "reask_exhausted"

        stale_dir = tmp_path / "stale_after_reask"
        stale_sink = configure_audit(Settings(_env_file=None, data_dir=str(stale_dir)))
        assert stale_sink is not None
        stale_posted: list[Decision] = []
        stale_approve_calls = 0

        def stale_recorder(decision: Decision) -> None:
            nonlocal stale_approve_calls
            if decision.approved:
                stale_approve_calls += 1
                if stale_approve_calls <= 2:
                    raise ApprovalPostError(
                        "approval no longer pending",
                        status=409,
                        code="approval_not_pending",
                    )
            stale_posted.append(decision)

        stale_gate = ApprovalGate(audit=True, on_decide=stale_recorder)
        stale_request = _request(prompt_id="prompt-stale", execution_generation=8)
        assert stale_gate.request(stale_request, now=20.0) == stale_request
        assert stale_gate.answer(_utterance(), control=stale_request.control_id, now=21.0) is None
        stale_denied = stale_gate.answer(
            _utterance(),
            control=stale_request.control_id,
            now=22.0,
        )
        assert stale_denied is not None
        assert stale_posted == [stale_denied]
        stale_decided = next(
            entry for entry in stale_sink.read_entries() if entry["event"] == "approval.decided"
        )
        assert stale_decided["prompt_id"] == stale_request.prompt_id
        assert stale_decided["execution_generation"] == stale_request.execution_generation
        assert stale_decided["decision"] == "denied"
        assert stale_decided["reason"] == "stale_after_reask"

        transport_dir = tmp_path / "transport_error"
        transport_sink = configure_audit(Settings(_env_file=None, data_dir=str(transport_dir)))
        assert transport_sink is not None
        transport_posted: list[Decision] = []

        def transport_recorder(decision: Decision) -> None:
            if decision.approved:
                raise ApprovalPostError("service unavailable", status=503)
            transport_posted.append(decision)

        transport_gate = ApprovalGate(audit=True, on_decide=transport_recorder)
        transport_request = _request(prompt_id="prompt-transport", execution_generation=9)
        assert transport_gate.request(transport_request, now=30.0) == transport_request
        transport_denied = transport_gate.answer(
            _utterance(),
            control=transport_request.control_id,
            now=31.0,
        )
        assert transport_denied is not None
        assert transport_posted == [transport_denied]
        transport_decided = next(
            entry for entry in transport_sink.read_entries() if entry["event"] == "approval.decided"
        )
        assert transport_decided["prompt_id"] == transport_request.prompt_id
        assert transport_decided["execution_generation"] == transport_request.execution_generation
        assert transport_decided["decision"] == "denied"
        assert transport_decided["reason"] == "transport_error"
    finally:
        reset_audit()


def test_from_settings_wires_hardening_values() -> None:
    settings = Settings(
        _env_file=None,
        approval_confirmation_phrase="yes run it",
        approval_min_confidence=0.8,
        approval_max_reasks=2,
        approval_echo_guard=False,
    )
    gate = ApprovalGate.from_settings(settings)
    assert gate.confirmation_phrase == "yes run it"
    assert gate.min_confidence == 0.8
    assert gate.max_reasks == 2
    assert gate.echo_guard is False
    assert gate.default_timeout_s == 20.0

    override_gate = ApprovalGate.from_settings(settings, max_reasks=5)
    assert override_gate.max_reasks == 5
