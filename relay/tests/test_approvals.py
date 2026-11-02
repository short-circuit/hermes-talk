"""Offline tests for the spoken approval gate policy.

The suite uses an injected fake clock (``now=`` values), recording sinks and
``tmp_path`` only.  It makes no network calls and never sleeps.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from relay.brain.approvals import (
    READ_ONLY_COMMANDS,
    ApprovalGate,
    ApprovalGateError,
    ApprovalRequest,
    ControlId,
    Decision,
    Provenance,
    StaleControlIdError,
    Utterance,
    is_read_only_command,
)
from relay.config import Settings
from relay.logging import configure_audit

_READ_ONLY = [
    "ls -la",
    "cat README.md",
    "git status",
    "git diff --stat",
    "grep -rn approval relay",
    "ls | head",
    "wc -l relay/brain/approvals.py",
    "find . -name '*.py'",
]

_NOT_READ_ONLY = [
    "rm -rf build",
    'git commit -m "x"',
    "sed -i s/a/b/ f",
    "echo hi > out.txt",
    "find . -delete",
    "sort -o out f",
    "tee f",
    "ls && rm x",
    "xargs rm",
    "python -m pytest",
    "",
]


def _request(
    *,
    prompt_id: str = "prompt-1",
    execution_generation: int = 1,
    command: str | None = None,
    timeout_s: float = 20.0,
    read_only: bool | None = None,
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
        read_only=read_only,
    )


def _recording_sink() -> tuple[list[Decision], object]:
    posted: list[Decision] = []

    def record(decision: Decision) -> None:
        posted.append(decision)

    return posted, record


def test_destructive_command_denied_on_silence() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request(command="rm -rf build", timeout_s=5.0)
    assert gate.request(request, now=100.0) == request
    assert gate.expire(now=104.0) is None
    assert gate.pending == request
    decision = gate.expire(now=106.0)
    assert decision is not None
    assert decision.approved is False
    assert decision.reason == "timeout"
    assert decision.wire_payload() == {
        "request_id": "prompt-1",
        "execution_generation": 1,
        "choice": "deny",
    }
    assert posted == [decision]
    assert gate.pending is None
    assert (
        gate.answer(Utterance(text="yes run it", provenance=Provenance.SPOKEN), now=107.0) is None
    )
    assert posted == [decision]


def test_readout_echoes_the_exact_command_and_hash() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    command = "rm -rf build"
    request = _request(command=command)
    assert gate.request(request, now=10.0) == request
    readout = gate.readout(now=11.0)
    assert readout.text.count(command) == 1
    assert command in readout.text
    assert readout.command == command
    assert readout.command_sha256 == hashlib.sha256(command.encode("utf-8")).hexdigest()
    assert readout.deadline_at == 10.0 + request.timeout_s
    assert readout.reask is False
    decision = gate.answer(
        Utterance(text="yes, run it", provenance=Provenance.SPOKEN),
        now=12.0,
    )
    assert decision is not None
    assert decision.command_sha256 == readout.command_sha256
    assert decision.prompt_id == request.prompt_id
    assert decision.execution_generation == request.execution_generation
    assert posted == [decision]

    other_command = "rm -rf dist"
    other = _request(prompt_id="prompt-2", execution_generation=2, command=other_command)
    assert gate.request(other, now=20.0) == other
    other_readout = gate.readout()
    assert other_readout.text != readout.text
    assert other_readout.command_sha256 != readout.command_sha256
    assert other_readout.command_sha256 == hashlib.sha256(other_command.encode("utf-8")).hexdigest()


def test_stale_control_id_rejected_and_reasked() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request(prompt_id="prompt-stale", execution_generation=4, command="rm -rf build")
    assert gate.request(request, now=100.0) == request

    wrong_prompt = ControlId("prompt-other", 4)
    with pytest.raises(StaleControlIdError) as prompt_error:
        gate.answer(
            Utterance(text="yes run it", provenance=Provenance.SPOKEN),
            control=wrong_prompt,
            now=101.0,
        )
    assert prompt_error.value.reason == "prompt_mismatch"
    assert prompt_error.value.expected == request.control_id
    assert prompt_error.value.received == wrong_prompt
    assert gate.pending == request
    assert gate.decision() is None
    assert gate.reask_count == 1
    assert posted == []

    reask = gate.reask(now=102.0)
    assert reask.reask is True
    assert reask.text.startswith("I didn't catch that. ")
    assert request.command in reask.text
    assert reask.deadline_at == 100.0 + request.timeout_s

    wrong_generation = ControlId("prompt-stale", 99)
    with pytest.raises(StaleControlIdError) as generation_error:
        gate.answer(
            Utterance(text="yes run it", provenance=Provenance.SPOKEN),
            control=wrong_generation,
            now=103.0,
        )
    assert generation_error.value.reason == "stale_generation"
    assert generation_error.value.expected == request.control_id
    assert generation_error.value.received == wrong_generation
    assert gate.reask_count == 2
    assert gate.pending == request
    assert posted == []

    decision = gate.answer(
        Utterance(text="yes, run it", provenance=Provenance.SPOKEN),
        control=request.control_id,
        now=104.0,
    )
    assert decision is not None
    assert decision.approved is True
    assert decision.wire_payload() == {
        "request_id": "prompt-stale",
        "execution_generation": 4,
        "choice": "once",
    }
    assert posted == [decision]


def test_stale_control_after_settled_prompt() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request(command="rm -rf build", timeout_s=5.0)
    assert gate.request(request, now=10.0) == request
    decision = gate.answer(Utterance(text="no", provenance=Provenance.SPOKEN), now=11.0)
    assert decision is not None
    assert decision.approved is False
    assert posted == [decision]

    wrong = ControlId("prompt-other", 1)
    with pytest.raises(StaleControlIdError) as stale_error:
        gate.answer(
            Utterance(text="yes run it", provenance=Provenance.SPOKEN),
            control=wrong,
            now=12.0,
        )
    assert stale_error.value.reason == "approval_not_pending"
    assert stale_error.value.expected == request.control_id
    assert stale_error.value.received == wrong
    assert (
        gate.answer(
            Utterance(text="yes run it", provenance=Provenance.SPOKEN),
            control=request.control_id,
            now=13.0,
        )
        is None
    )
    assert gate.decision() == decision
    assert gate.pending is None
    assert posted == [decision]


@pytest.mark.parametrize("command", _READ_ONLY)
def test_read_only_commands_auto_approve(command: str) -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request(command=command)
    result = gate.request(request, now=10.0)
    assert isinstance(result, Decision)
    assert result.approved is True
    assert result.reason == "read_only_allowlist"
    assert result.command_sha256 == hashlib.sha256(command.encode("utf-8")).hexdigest()
    assert result.wire_payload()["choice"] == "once"
    assert gate.pending is None
    assert gate.decision() == result
    assert posted == [result]


@pytest.mark.parametrize("command", _NOT_READ_ONLY)
def test_non_read_only_commands_stay_pending(command: str) -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request(command=command)
    assert gate.request(request, now=10.0) == request
    assert gate.pending == request
    assert gate.decision() is None
    assert posted == []


def test_auto_approve_disabled_prompts_even_read_only_commands() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, auto_approve_read_only=False, on_decide=sink)
    request = _request(command="ls -la")
    assert gate.request(request, now=10.0) == request
    assert gate.pending == request
    assert posted == []


def test_explicit_read_only_flag_overrides_command_inference() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request(command="rm -rf build", read_only=True)
    result = gate.request(request, now=10.0)
    assert isinstance(result, Decision)
    assert result.reason == "read_only_allowlist"
    assert posted == [result]

    posted_again, sink_again = _recording_sink()
    gate_again = ApprovalGate(audit=False, on_decide=sink_again)
    not_read_only = _request(command="ls -la", read_only=False)
    assert gate_again.request(not_read_only, now=10.0) == not_read_only
    assert gate_again.pending == not_read_only
    assert posted_again == []


def test_commandless_request_is_never_auto_approved() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request(command=None, read_only=True)
    assert gate.request(request, now=10.0) == request
    assert gate.pending == request
    assert posted == []


def test_low_confidence_denies_and_the_floor_approves() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, min_confidence=0.6, on_decide=sink)
    request = _request(command="rm -rf build")
    assert gate.request(request, now=10.0) == request
    decision = gate.answer(
        Utterance(text="yes, run it", provenance=Provenance.SPOKEN, confidence=0.2),
        now=11.0,
    )
    assert decision is not None
    assert decision.approved is False
    assert decision.reason == "low_confidence"
    assert decision.choice == "deny"
    assert posted == [decision]

    posted_floor, sink_floor = _recording_sink()
    gate_floor = ApprovalGate(audit=False, min_confidence=0.6, on_decide=sink_floor)
    request_floor = _request(
        prompt_id="prompt-floor", execution_generation=2, command="rm -rf build"
    )
    assert gate_floor.request(request_floor, now=10.0) == request_floor
    approved = gate_floor.answer(
        Utterance(text="yes, run it", provenance=Provenance.SPOKEN, confidence=0.6),
        now=11.0,
    )
    assert approved is not None
    assert approved.approved is True
    assert posted_floor == [approved]

    with pytest.raises(ValueError):
        Utterance(text="yes", provenance=Provenance.SPOKEN, confidence=1.1)
    with pytest.raises(ValueError):
        Utterance(text="yes", provenance=Provenance.SPOKEN, confidence=-0.1)


@pytest.mark.parametrize(
    "provenance",
    [Provenance.DOCUMENT, Provenance.TOOL_OUTPUT, Provenance.MODEL],
)
def test_non_spoken_provenance_cannot_approve(provenance: Provenance) -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request(command="rm -rf build")
    assert gate.request(request, now=10.0) == request
    assert (
        gate.answer(
            Utterance(text="yes run it", provenance=provenance),
            now=11.0,
        )
        is None
    )
    assert gate.pending == request
    assert gate.decision() is None
    assert posted == []


def test_bare_yes_denies_with_phrase_mismatch() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request(command="rm -rf build")
    assert gate.request(request, now=10.0) == request
    decision = gate.answer(Utterance(text="yes", provenance=Provenance.SPOKEN), now=11.0)
    assert decision is not None
    assert decision.approved is False
    assert decision.reason == "phrase_mismatch"
    assert decision.choice == "deny"
    assert posted == [decision]


def test_no_denies_with_spoken_negation() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request(command="rm -rf build")
    assert gate.request(request, now=10.0) == request
    decision = gate.answer(Utterance(text="no", provenance=Provenance.SPOKEN), now=11.0)
    assert decision is not None
    assert decision.approved is False
    assert decision.reason == "spoken_negation"
    assert decision.choice == "deny"
    assert decision.wire_payload()["choice"] == "deny"
    assert posted == [decision]


def test_exactly_once_posting_on_duplicate_and_late_answers() -> None:
    posted, sink = _recording_sink()
    gate = ApprovalGate(audit=False, on_decide=sink)
    request = _request(command="rm -rf build")
    assert gate.request(request, now=10.0) == request
    decision = gate.answer(Utterance(text="yes run it", provenance=Provenance.SPOKEN), now=11.0)
    assert decision is not None
    assert len(posted) == 1
    assert gate.answer(Utterance(text="no", provenance=Provenance.SPOKEN), now=12.0) is None
    assert (
        gate.answer(
            Utterance(text="yes run it", provenance=Provenance.SPOKEN),
            control=request.control_id,
            now=13.0,
        )
        is None
    )
    assert len(posted) == 1
    assert gate.decision() == decision


def test_sink_exception_is_swallowed_and_recorded() -> None:
    def boom(_decision: Decision) -> None:
        raise RuntimeError("sink failed")

    gate = ApprovalGate(audit=False, on_decide=boom)
    request = _request(command="rm -rf build")
    assert gate.request(request, now=10.0) == request
    decision = gate.answer(Utterance(text="yes run it", provenance=Provenance.SPOKEN), now=11.0)
    assert decision is not None
    assert decision.approved is True
    assert gate.decision() == decision
    assert gate.post_error == "sink failed"
    assert gate.answer(Utterance(text="no", provenance=Provenance.SPOKEN), now=12.0) is None
    assert gate.post_error == "sink failed"


def test_backward_compatible_constructor_and_request_answer_shapes() -> None:
    gate = ApprovalGate()
    request = _request(command=None)
    assert gate.request(request, now=10.0) == request
    assert gate.pending == request
    second = _request(prompt_id="prompt-2", execution_generation=2)
    with pytest.raises(ApprovalGateError):
        gate.request(second, now=11.0)
    decision = gate.answer(Utterance(text="yes run it", provenance=Provenance.SPOKEN), now=12.0)
    assert decision is not None
    assert decision.approved is True
    assert decision.reason == "spoken_phrase"


def test_read_only_allowlist_omits_mutating_binaries() -> None:
    excluded = {
        "tee",
        "sed",
        "awk",
        "xargs",
        "rm",
        "mv",
        "cp",
        "mkdir",
        "touch",
        "chmod",
        "install",
        "curl",
        "wget",
        "python",
    }
    assert excluded.isdisjoint(READ_ONLY_COMMANDS)


@pytest.mark.parametrize(
    "command",
    ["FOO=bar ls -la", "git remote -v", "git remote --verbose", "uniq -c file"],
)
def test_is_read_only_command_accepts_conservative_shapes(command: str) -> None:
    assert is_read_only_command(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "git remote",
        "git -C repo status",
        "find . -exec rm {} \\;",
        "sort --output=out f",
        "uniq file out",
        "echo 'unclosed",
    ],
)
def test_is_read_only_command_rejects_unsafe_shapes(command: str) -> None:
    assert is_read_only_command(command) is False


def test_audit_records_command_hash_and_denial_reason(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=str(tmp_path))
    sink = configure_audit(settings)
    assert sink is not None
    gate = ApprovalGate(audit=True)
    command = "rm -rf build"
    request = _request(command=command, timeout_s=5.0)
    assert gate.request(request, now=10.0) == request
    decision = gate.answer(Utterance(text="no", provenance=Provenance.SPOKEN), now=11.0)
    assert decision is not None
    entries = sink.read_entries()
    requested = next(entry for entry in entries if entry["event"] == "approval.requested")
    decided = next(entry for entry in entries if entry["event"] == "approval.decided")
    expected_hash = hashlib.sha256(command.encode("utf-8")).hexdigest()
    assert requested["command_sha256"] == expected_hash
    assert decided["command_sha256"] == expected_hash
    assert decided["reason"] == "spoken_negation"
    assert decided["decision"] == "denied"


def test_new_names_are_exported_from_brain() -> None:
    import relay.brain as brain

    for name in (
        "ControlId",
        "PromptReadout",
        "READ_ONLY_COMMANDS",
        "StaleControlIdError",
        "is_read_only_command",
    ):
        assert name in brain.__all__
        assert hasattr(brain, name)
