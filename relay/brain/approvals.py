"""Spoken approval gate for destructive tool calls.

The spoken gate is the only approver.  Document text, tool output, and the
model's own agreement can never satisfy an approval.  The gate defaults to
deny, allows one pending prompt per run, binds every decision to the wire
``prompt_id``/``execution_generation`` pair, and audits every request and
decision.  Provably read-only commands can be auto-approved.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum

from relay.logging import (
    audit_approval_decided,
    audit_approval_requested,
    transcript_hash,
)

_NEGATION_TOKENS = frozenset({"no", "not", "dont", "cancel", "stop"})
_PUNCTUATION_RE = re.compile(r"[^a-z0-9\s]")
_APOSTROPHES = ("'", "\u2019")
_SHELL_METACHARACTERS = (">", "<", "$(", "`", ";", "&&", "||", "&", "\n")
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_GIT_READ_ONLY_SUBCOMMANDS = frozenset(
    {
        "status",
        "diff",
        "log",
        "show",
        "rev-parse",
        "describe",
        "blame",
        "ls-files",
        "ls-tree",
        "cat-file",
        "shortlog",
        "whatchanged",
        "grep",
    }
)
_FIND_SIDE_EFFECT_FLAGS = frozenset(
    {
        "-delete",
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-fprint",
        "-fprintf",
        "-fls",
    }
)
_UNIQ_READ_ONLY_FLAGS = frozenset(
    {
        "-c",
        "-d",
        "-u",
        "--count",
        "--repeated",
        "--unique",
    }
)

READ_ONLY_COMMANDS: tuple[str, ...] = (
    "ls",
    "cat",
    "head",
    "tail",
    "pwd",
    "wc",
    "grep",
    "rg",
    "file",
    "stat",
    "du",
    "df",
    "tree",
    "git",
    "date",
    "echo",
    "printf",
    "diff",
    "jq",
    "sha256sum",
    "which",
    "uname",
    "id",
    "nproc",
    "uptime",
    "column",
    "sort",
    "uniq",
    "cut",
    "tr",
    "find",
)


class Provenance(StrEnum):
    """Where an utterance came from; only ``SPOKEN`` can approve."""

    SPOKEN = "spoken"
    TOOL_OUTPUT = "tool_output"
    DOCUMENT = "document"
    MODEL = "model"


@dataclass(frozen=True)
class Utterance:
    """One utterance with its provenance and ASR confidence."""

    text: str
    provenance: Provenance
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            msg = f"confidence must be between 0.0 and 1.0, got {self.confidence}"
            raise ValueError(msg)


@dataclass(frozen=True)
class ControlId:
    """The wire identity of one approval prompt."""

    prompt_id: str
    execution_generation: int


@dataclass(frozen=True)
class ApprovalRequest:
    """One pending approval prompt for a specific tool action."""

    prompt_id: str
    execution_generation: int
    tool: str
    action_summary: str
    required_phrase: str
    timeout_s: float = 20.0
    command: str | None = None
    description: str | None = None
    run_id: str | None = None
    read_only: bool | None = None

    @property
    def control_id(self) -> ControlId:
        """Return the prompt identity echoed on the wire."""
        return ControlId(self.prompt_id, self.execution_generation)


@dataclass(frozen=True)
class PromptReadout:
    """The spoken prompt text and the ids needed to answer it."""

    text: str
    prompt_id: str
    execution_generation: int
    command: str | None
    command_sha256: str
    deadline_at: float
    reask: bool = False


@dataclass(frozen=True)
class Decision:
    """The recorded approval decision; it is final once returned."""

    approved: bool
    reason: str
    prompt_id: str
    execution_generation: int
    actor: str = "spoken_gate"
    command_sha256: str = ""

    @property
    def choice(self) -> str:
        """Return the exact wire choice vocabulary for this decision."""
        return "once" if self.approved else "deny"

    def wire_payload(self) -> dict[str, str | int]:
        """Return the exact three-key body accepted by the approval endpoint."""
        return {
            "request_id": self.prompt_id,
            "execution_generation": self.execution_generation,
            "choice": self.choice,
        }


class ApprovalGateError(Exception):
    """A second prompt was requested while one is still pending."""


class StaleControlIdError(ApprovalGateError):
    """An answer carried a control id that does not match the pending prompt."""

    reason: str
    expected: ControlId | None
    received: ControlId | None

    def __init__(
        self,
        reason: str,
        expected: ControlId | None = None,
        received: ControlId | None = None,
    ) -> None:
        self.reason = reason
        self.expected = expected
        self.received = received
        super().__init__(reason)


def _normalize_tokens(text: str) -> list[str]:
    lowered = text.lower()
    for apostrophe in _APOSTROPHES:
        lowered = lowered.replace(apostrophe, "")
    cleaned = _PUNCTUATION_RE.sub(" ", lowered)
    return cleaned.split()


def _contains_token_sequence(tokens: list[str], phrase_tokens: list[str]) -> bool:
    if not phrase_tokens:
        return True
    width = len(phrase_tokens)
    return any(
        tokens[index : index + width] == phrase_tokens for index in range(len(tokens) - width + 1)
    )


def _command_sha256(command: str | None) -> str:
    if command is None:
        return ""
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def _readout(request: ApprovalRequest) -> str:
    if request.command is None:
        return f"{request.action_summary}. Say {request.required_phrase} to confirm."
    return (
        f"{request.action_summary}. Running: {request.command}. "
        f'Say "{request.required_phrase}" to confirm.'
    )


def _git_is_read_only(arguments: list[str]) -> bool:
    if not arguments:
        return False
    subcommand = arguments[0]
    if subcommand in _GIT_READ_ONLY_SUBCOMMANDS:
        return True
    return subcommand == "remote" and len(arguments) == 2 and arguments[1] in {"-v", "--verbose"}


def _sort_writes(token: str) -> bool:
    if token in {"-o", "--output"}:
        return True
    if token.startswith("--output="):
        return True
    return token.startswith("-o") and len(token) > 2


def _uniq_is_read_only(arguments: list[str]) -> bool:
    positional = 0
    for token in arguments:
        if token.startswith("-"):
            if token not in _UNIQ_READ_ONLY_FLAGS:
                return False
        else:
            positional += 1
    return positional <= 1


def _segment_is_read_only(binary: str, arguments: list[str]) -> bool:
    if binary == "git":
        return _git_is_read_only(arguments)
    if binary == "find":
        return not any(token in _FIND_SIDE_EFFECT_FLAGS for token in arguments)
    if binary == "sort":
        return not any(_sort_writes(token) for token in arguments)
    if binary == "uniq":
        return _uniq_is_read_only(arguments)
    return True


def is_read_only_command(command: str | None) -> bool:
    """Return ``True`` only when *command* is provably side-effect free."""
    if command is None:
        return False
    if not command.strip():
        return False
    if any(character in command for character in _SHELL_METACHARACTERS):
        return False
    segments = command.split("|")
    if not segments:
        return False
    for segment in segments:
        if not segment.strip():
            return False
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return False
        if not tokens:
            return False
        index = 0
        while index < len(tokens) and _ENV_ASSIGNMENT_RE.match(tokens[index]):
            index += 1
        if index >= len(tokens):
            return False
        binary = tokens[index]
        if binary not in READ_ONLY_COMMANDS:
            return False
        if not _segment_is_read_only(binary, tokens[index + 1 :]):
            return False
    return True


class ApprovalGate:
    """A one-prompt-at-a-time gate that only accepts spoken confirmation."""

    def __init__(
        self,
        *,
        min_confidence: float = 0.6,
        audit: bool = True,
        auto_approve_read_only: bool = True,
        on_decide: Callable[[Decision], None] | None = None,
    ) -> None:
        self.min_confidence = min_confidence
        self.audit = audit
        self.auto_approve_read_only = auto_approve_read_only
        self.on_decide = on_decide
        self._request: ApprovalRequest | None = None
        self._requested_at: float | None = None
        self._decision: Decision | None = None
        self.reask_count = 0
        self.post_error: str | None = None

    @property
    def pending(self) -> ApprovalRequest | None:
        """Return the pending request, or ``None`` once a decision exists."""
        if self._decision is not None:
            return None
        return self._request

    def _should_auto_approve(self, request: ApprovalRequest) -> bool:
        if not self.auto_approve_read_only:
            return False
        if request.command is None:
            return False
        if request.read_only is not None:
            return request.read_only
        return is_read_only_command(request.command)

    def request(
        self, request: ApprovalRequest, *, now: float | None = None
    ) -> ApprovalRequest | Decision:
        """Record a new prompt; refuse a second prompt while one is pending."""
        pending = self.pending
        if pending is not None:
            msg = f"approval already pending: {pending.prompt_id}"
            raise ApprovalGateError(msg)
        self._request = request
        self._requested_at = time.monotonic() if now is None else now
        self._decision = None
        self.reask_count = 0
        self.post_error = None
        if self.audit:
            audit_approval_requested(
                prompt_id=request.prompt_id,
                execution_generation=request.execution_generation,
                transcript_sha256=transcript_hash(_readout(request)),
                tool=request.tool,
                summary=request.action_summary,
                command_sha256=_command_sha256(request.command),
            )
        if self._should_auto_approve(request):
            decision = Decision(
                approved=True,
                reason="read_only_allowlist",
                prompt_id=request.prompt_id,
                execution_generation=request.execution_generation,
                command_sha256=_command_sha256(request.command),
            )
            self._decision = decision
            self._post(decision, utterance_text=None)
            return decision
        return request

    def readout(self, *, now: float | None = None) -> PromptReadout:
        """Return the spoken prompt for the pending request."""
        del now  # the deadline is anchored to the original request time
        request = self.pending
        if request is None:
            msg = "no pending approval"
            raise ApprovalGateError(msg)
        requested_at = self._requested_at
        if requested_at is None:
            msg = "approval request has no timestamp"
            raise ApprovalGateError(msg)
        return PromptReadout(
            text=_readout(request),
            prompt_id=request.prompt_id,
            execution_generation=request.execution_generation,
            command=request.command,
            command_sha256=_command_sha256(request.command),
            deadline_at=requested_at + request.timeout_s,
            reask=False,
        )

    def reask(self, *, now: float | None = None) -> PromptReadout:
        """Return a re-ask readout without extending the original deadline."""
        readout = self.readout(now=now)
        return replace(readout, text=f"I didn't catch that. {readout.text}", reask=True)

    @staticmethod
    def _received_control(
        *,
        control: ControlId | None,
        prompt_id: str | None,
        execution_generation: int | None,
    ) -> ControlId | None:
        if control is not None:
            return control
        if prompt_id is None and execution_generation is None:
            return None
        if prompt_id is None or execution_generation is None:
            msg = "prompt_id and execution_generation must be supplied together"
            raise ValueError(msg)
        return ControlId(prompt_id, execution_generation)

    @staticmethod
    def _matches_decision(received: ControlId, decision: Decision) -> bool:
        return (
            received.prompt_id == decision.prompt_id
            and received.execution_generation == decision.execution_generation
        )

    def answer(
        self,
        utterance: Utterance,
        *,
        control: ControlId | None = None,
        prompt_id: str | None = None,
        execution_generation: int | None = None,
        now: float | None = None,
    ) -> Decision | None:
        """Decide a pending prompt from a spoken utterance, or return ``None``."""
        received = self._received_control(
            control=control,
            prompt_id=prompt_id,
            execution_generation=execution_generation,
        )
        if received is not None:
            pending = self.pending
            if pending is not None:
                expected = pending.control_id
                if received.prompt_id != expected.prompt_id:
                    self.reask_count += 1
                    raise StaleControlIdError(
                        "prompt_mismatch", expected=expected, received=received
                    )
                if received.execution_generation != expected.execution_generation:
                    self.reask_count += 1
                    raise StaleControlIdError(
                        "stale_generation", expected=expected, received=received
                    )
            else:
                settled = self._decision
                if settled is not None and self._matches_decision(received, settled):
                    return None
                expected = (
                    ControlId(settled.prompt_id, settled.execution_generation)
                    if settled is not None
                    else None
                )
                raise StaleControlIdError(
                    "approval_not_pending",
                    expected=expected,
                    received=received,
                )
        if self._decision is not None:
            return None
        request = self._request
        if request is None:
            return None
        current = time.monotonic() if now is None else now
        expired = self.expire(now=current)
        if expired is not None:
            return expired
        if utterance.provenance != Provenance.SPOKEN:
            return None
        if utterance.confidence < self.min_confidence:
            decision = Decision(
                approved=False,
                reason="low_confidence",
                prompt_id=request.prompt_id,
                execution_generation=request.execution_generation,
                command_sha256=_command_sha256(request.command),
            )
            self._decision = decision
            self._post(decision, utterance_text=utterance.text)
            return decision
        tokens = _normalize_tokens(utterance.text)
        phrase_tokens = _normalize_tokens(request.required_phrase)
        negation = any(token in _NEGATION_TOKENS for token in tokens)
        phrase_match = _contains_token_sequence(tokens, phrase_tokens)
        if phrase_match and not negation:
            approved = True
            reason = "spoken_phrase"
        elif negation:
            approved = False
            reason = "spoken_negation"
        else:
            approved = False
            reason = "phrase_mismatch"
        decision = Decision(
            approved=approved,
            reason=reason,
            prompt_id=request.prompt_id,
            execution_generation=request.execution_generation,
            command_sha256=_command_sha256(request.command),
        )
        self._decision = decision
        self._post(decision, utterance_text=utterance.text)
        return decision

    def expire(self, *, now: float | None = None) -> Decision | None:
        """Deny a pending prompt once its timeout has passed."""
        if self._decision is not None or self._request is None:
            return None
        requested_at = self._requested_at
        if requested_at is None:
            return None
        current = time.monotonic() if now is None else now
        request = self._request
        deadline = requested_at + request.timeout_s
        if current <= deadline:
            return None
        decision = Decision(
            approved=False,
            reason="timeout",
            prompt_id=request.prompt_id,
            execution_generation=request.execution_generation,
            command_sha256=_command_sha256(request.command),
        )
        self._decision = decision
        self._post(decision, utterance_text=None)
        return decision

    def _post(self, decision: Decision, *, utterance_text: str | None) -> None:
        if self.audit:
            if decision.reason == "timeout":
                audit_decision = "timeout"
            else:
                audit_decision = "approved" if decision.approved else "denied"
            audit_approval_decided(
                prompt_id=decision.prompt_id,
                execution_generation=decision.execution_generation,
                decision=audit_decision,
                transcript_sha256=(
                    transcript_hash(utterance_text) if utterance_text is not None else None
                ),
                actor="spoken_gate",
                reason=decision.reason,
                command_sha256=decision.command_sha256,
            )
        if self.on_decide is None:
            return
        try:
            self.on_decide(decision)
        except Exception as exc:
            self.post_error = str(exc)

    def decision(self) -> Decision | None:
        """Return the recorded decision, or ``None`` while undecided."""
        return self._decision


__all__ = [
    "READ_ONLY_COMMANDS",
    "ApprovalGate",
    "ApprovalGateError",
    "ApprovalRequest",
    "ControlId",
    "Decision",
    "PromptReadout",
    "Provenance",
    "StaleControlIdError",
    "Utterance",
    "is_read_only_command",
]
