"""Spoken approval gate for destructive tool calls.

The spoken gate is the only approver.  Document text, tool output, and the
model's own agreement can never satisfy an approval.  The gate defaults to
deny, allows one pending prompt per run, binds every decision to the wire
``prompt_id``/``execution_generation`` pair, and audits every request and
decision.  Provably read-only commands can be auto-approved.

Denial reasons emitted by this module include ``timeout``, ``low_confidence``,
``phrase_mismatch``, ``spoken_negation``, ``overlapping_speakers``,
``ambiguous_phrase``, ``reask_exhausted``, ``stale_after_reask``, and
``transport_error``.  A playback window can suppress the relay's own TTS echo
and ambient speech captured before the readout finished.
"""

from __future__ import annotations

import hashlib
import math
import re
import shlex
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType

from relay.config import Settings
from relay.logging import (
    audit_approval_decided,
    audit_approval_ignored,
    audit_approval_reasked,
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
    """One utterance with its provenance, ASR confidence, and acoustic metadata."""

    text: str
    provenance: Provenance
    confidence: float = 1.0
    onset_at: float | None = None
    from_playback_frames: bool = False
    speaker_count: int = 1

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            msg = f"confidence must be between 0.0 and 1.0, got {self.confidence}"
            raise ValueError(msg)
        if self.speaker_count < 1:
            msg = f"speaker_count must be at least 1, got {self.speaker_count}"
            raise ValueError(msg)
        if self.onset_at is not None and (not math.isfinite(self.onset_at) or self.onset_at < 0.0):
            msg = f"onset_at must be finite and zero or greater, got {self.onset_at}"
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


@dataclass
class PlaybackWindow:
    """The readout playback lifecycle for the current approval prompt."""

    started_at: float | None = None
    closed_at: float | None = None
    closed_by: str | None = None

    @property
    def active(self) -> bool:
        """Return ``True`` while playback has started and has not been closed."""
        return self.started_at is not None and self.closed_at is None


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


class ApprovalPostError(Exception):
    """A decision sink failed to POST a decision to the wire endpoint."""

    status: int | None
    code: str

    def __init__(self, message: str, *, status: int | None = None, code: str = "") -> None:
        self.status = status
        self.code = code
        super().__init__(message)

    @property
    def stale_conflict(self) -> bool:
        """Return ``True`` for a stale 409 the caller may safely re-ask."""
        return self.status == 409 and self.code in {"approval_not_pending", "stale_generation"}


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


def _readout(request: ApprovalRequest, phrase: str | None = None) -> str:
    confirmation_phrase = phrase if phrase is not None else request.required_phrase
    if request.command is None:
        return f"{request.action_summary}. Say {confirmation_phrase} to confirm."
    return (
        f"{request.action_summary}. Running: {request.command}. "
        f'Say "{confirmation_phrase}" to confirm.'
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


_MAX_SETTLED_DECISIONS = 32


class ApprovalGate:
    """A one-prompt-at-a-time gate that only accepts spoken confirmation."""

    def __init__(
        self,
        *,
        min_confidence: float = 0.6,
        audit: bool = True,
        auto_approve_read_only: bool = True,
        on_decide: Callable[[Decision], None] | None = None,
        confirmation_phrase: str | None = None,
        max_reasks: int = 1,
        echo_guard: bool = True,
    ) -> None:
        if confirmation_phrase is not None and len(_normalize_tokens(confirmation_phrase)) < 2:
            msg = "confirmation_phrase must contain at least two tokens"
            raise ValueError(msg)
        if max_reasks < 0:
            msg = "max_reasks must be zero or greater"
            raise ValueError(msg)
        self.min_confidence = min_confidence
        self.audit = audit
        self.auto_approve_read_only = auto_approve_read_only
        self.on_decide = on_decide
        self.confirmation_phrase = confirmation_phrase
        self.max_reasks = max_reasks
        self.echo_guard = echo_guard
        self.default_timeout_s: float | None = None
        self._request: ApprovalRequest | None = None
        self._requested_at: float | None = None
        self._decision: Decision | None = None
        self.reask_count = 0
        self.post_error: str | None = None
        self._playback = PlaybackWindow()
        self._needs_reask = False
        self._reask_reasons: dict[str, int] = {}
        self._ignored_count = 0
        self._last_ignored_reason: str | None = None
        self._settled: dict[str, Decision] = {}

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: object) -> ApprovalGate:
        """Build a gate from :class:`Settings`, applying keyword overrides last.

        ``default_timeout_s`` is exposed for callers that want to copy the
        configured timeout into a new :class:`ApprovalRequest`.
        """
        kwargs: dict[str, object] = {
            "confirmation_phrase": settings.approval_confirmation_phrase,
            "min_confidence": settings.approval_min_confidence,
            "max_reasks": settings.approval_max_reasks,
            "echo_guard": settings.approval_echo_guard,
        }
        default_timeout_s = overrides.pop("default_timeout_s", settings.approval_timeout_s)
        kwargs.update(overrides)
        gate = cls(**kwargs)  # type: ignore[arg-type]
        gate.default_timeout_s = float(default_timeout_s)
        return gate

    @property
    def pending(self) -> ApprovalRequest | None:
        """Return the pending request, or ``None`` once a decision exists."""
        if self._decision is not None:
            return None
        return self._request

    @property
    def playback_window(self) -> PlaybackWindow:
        """Return a copy of the current readout playback window."""
        return replace(self._playback)

    @property
    def playback_closed_at(self) -> float | None:
        """Return the time the current playback window closed, if it has closed."""
        return self._playback.closed_at

    @property
    def needs_reask(self) -> bool:
        """Return ``True`` when the last attempt must be re-asked."""
        return self._needs_reask and self._decision is None

    @property
    def ignored_count(self) -> int:
        """Return the number of echo or ambient utterances ignored this prompt."""
        return self._ignored_count

    @property
    def last_ignored_reason(self) -> str | None:
        """Return the reason for the most recently ignored utterance."""
        return self._last_ignored_reason

    @property
    def reask_reasons(self) -> Mapping[str, int]:
        """Return a read-only copy of per-reason re-ask counters."""
        return MappingProxyType(dict(self._reask_reasons))

    def decision_for(
        self, prompt_id: str, execution_generation: int | None = None
    ) -> Decision | None:
        """Return a settled decision from the bounded replay registry."""
        decision = self._settled.get(prompt_id)
        if decision is None:
            return None
        if execution_generation is not None and (
            decision.execution_generation != execution_generation
        ):
            return None
        return decision

    def _should_auto_approve(self, request: ApprovalRequest) -> bool:
        if not self.auto_approve_read_only:
            return False
        if request.command is None:
            return False
        if request.read_only is not None:
            return request.read_only
        return is_read_only_command(request.command)

    def _effective_phrase(self, request: ApprovalRequest) -> str:
        return self.confirmation_phrase or request.required_phrase

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
        self._playback = PlaybackWindow()
        self._needs_reask = False
        self._reask_reasons.clear()
        self._ignored_count = 0
        self._last_ignored_reason = None
        if self.audit:
            audit_approval_requested(
                prompt_id=request.prompt_id,
                execution_generation=request.execution_generation,
                transcript_sha256=transcript_hash(
                    _readout(request, self._effective_phrase(request))
                ),
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
            settled = self._settle(decision, utterance_text=None)
            return settled if settled is not None else request
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
            text=_readout(request, self._effective_phrase(request)),
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

    def playback_started(self, *, now: float | None = None) -> None:
        """Record that the readout TTS started; no-op without a pending prompt."""
        if self.pending is None:
            return
        current = time.monotonic() if now is None else now
        self._playback = PlaybackWindow(started_at=current)

    def playback_finished(self, *, now: float | None = None) -> None:
        """Close an open playback window because the readout finished."""
        if not self._playback.active:
            return
        current = time.monotonic() if now is None else now
        self._playback.closed_at = current
        self._playback.closed_by = "finished"

    def barge_in(self, *, now: float | None = None, onset_at: float | None = None) -> None:
        """Close an open playback window at the interrupting speech onset."""
        if not self._playback.active:
            return
        current = time.monotonic() if now is None else now
        cut_at = min(onset_at, current) if onset_at is not None else current
        self._playback.closed_at = cut_at
        self._playback.closed_by = "barge_in"

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

    def _record_reask(self, reason: str) -> int:
        self.reask_count += 1
        count = self._reask_reasons.get(reason, 0) + 1
        self._reask_reasons[reason] = count
        return count

    def _latest_settled_control(self) -> ControlId | None:
        if not self._settled:
            return None
        latest = next(reversed(self._settled.values()))
        return ControlId(latest.prompt_id, latest.execution_generation)

    def _ignore(self, request: ApprovalRequest, reason: str, utterance: Utterance) -> None:
        self._ignored_count += 1
        self._last_ignored_reason = reason
        if self.audit:
            audit_approval_ignored(
                prompt_id=request.prompt_id,
                execution_generation=request.execution_generation,
                reason=reason,
                transcript_sha256=transcript_hash(utterance.text),
            )
        return None

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
                reason: str | None = None
                if received.prompt_id != expected.prompt_id:
                    reason = "prompt_mismatch"
                elif received.execution_generation != expected.execution_generation:
                    reason = "stale_generation"
                if reason is not None:
                    count = self._record_reask(reason)
                    if count > self.max_reasks:
                        decision = Decision(
                            approved=False,
                            reason="reask_exhausted",
                            prompt_id=pending.prompt_id,
                            execution_generation=pending.execution_generation,
                            command_sha256=_command_sha256(pending.command),
                        )
                        settled = self._settle(decision, utterance_text=utterance.text)
                        return settled if settled is not None else self._decision
                    self._needs_reask = True
                    raise StaleControlIdError(reason, expected=expected, received=received)
            else:
                settled = self._settled.get(received.prompt_id)
                if settled is not None and self._matches_decision(received, settled):
                    return None
                raise StaleControlIdError(
                    "approval_not_pending",
                    expected=self._latest_settled_control(),
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
        if self.echo_guard and self._playback.started_at is not None:
            if utterance.from_playback_frames:
                return self._ignore(request, "playback_frames", utterance)
            if self._playback.active:
                return self._ignore(request, "playback_active", utterance)
            closed_at = self._playback.closed_at
            if closed_at is not None:
                if utterance.onset_at is None:
                    return self._ignore(request, "onset_unknown", utterance)
                if utterance.onset_at < closed_at:
                    return self._ignore(request, "onset_before_playback_end", utterance)
        if utterance.confidence < self.min_confidence:
            return self._deny(request, "low_confidence", utterance_text=utterance.text)
        if utterance.speaker_count > 1:
            return self._deny(request, "overlapping_speakers", utterance_text=utterance.text)
        phrase_tokens = _normalize_tokens(self._effective_phrase(request))
        if len(phrase_tokens) < 2:
            return self._deny(request, "ambiguous_phrase", utterance_text=utterance.text)
        tokens = _normalize_tokens(utterance.text)
        negation = any(token in _NEGATION_TOKENS for token in tokens)
        phrase_match = _contains_token_sequence(tokens, phrase_tokens)
        if phrase_match and not negation:
            decision = Decision(
                approved=True,
                reason="spoken_phrase",
                prompt_id=request.prompt_id,
                execution_generation=request.execution_generation,
                command_sha256=_command_sha256(request.command),
            )
        elif negation:
            return self._deny(request, "spoken_negation", utterance_text=utterance.text)
        else:
            return self._deny(request, "phrase_mismatch", utterance_text=utterance.text)
        return self._settle(decision, utterance_text=utterance.text)

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
        return self._deny(request, "timeout", utterance_text=None)

    def _deny(
        self, request: ApprovalRequest, reason: str, *, utterance_text: str | None
    ) -> Decision | None:
        decision = Decision(
            approved=False,
            reason=reason,
            prompt_id=request.prompt_id,
            execution_generation=request.execution_generation,
            command_sha256=_command_sha256(request.command),
        )
        return self._settle(decision, utterance_text=utterance_text)

    def _register_settled(self, decision: Decision) -> None:
        self._settled.pop(decision.prompt_id, None)
        self._settled[decision.prompt_id] = decision
        while len(self._settled) > _MAX_SETTLED_DECISIONS:
            oldest = next(iter(self._settled))
            del self._settled[oldest]

    def _drop_settled(self, prompt_id: str) -> None:
        self._settled.pop(prompt_id, None)

    def _audit_decision(self, decision: Decision, *, utterance_text: str | None) -> None:
        if not self.audit:
            return
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

    def _try_post(self, decision: Decision) -> str | None:
        if self.on_decide is None:
            return None
        try:
            self.on_decide(decision)
        except Exception as exc:
            return str(exc)
        return None

    def _settle(self, decision: Decision, *, utterance_text: str | None) -> Decision | None:
        self._decision = decision
        self._register_settled(decision)
        self._needs_reask = False
        if self.on_decide is None:
            self._audit_decision(decision, utterance_text=utterance_text)
            return decision
        try:
            self.on_decide(decision)
        except ApprovalPostError as exc:
            self.post_error = str(exc)
            if exc.stale_conflict:
                if decision.approved:
                    count = self._record_reask(exc.code)
                    if count <= self.max_reasks:
                        self._decision = None
                        self._drop_settled(decision.prompt_id)
                        self._needs_reask = True
                        if self.audit:
                            audit_approval_reasked(
                                prompt_id=decision.prompt_id,
                                execution_generation=decision.execution_generation,
                                reason=exc.code,
                            )
                        return None
                deny = Decision(
                    approved=False,
                    reason="stale_after_reask" if decision.approved else decision.reason,
                    prompt_id=decision.prompt_id,
                    execution_generation=decision.execution_generation,
                    command_sha256=decision.command_sha256,
                )
                self._decision = deny
                self._register_settled(deny)
                error = self._try_post(deny)
                if error is not None:
                    self.post_error = error
                self._audit_decision(deny, utterance_text=utterance_text)
                return deny
            if decision.approved:
                deny = Decision(
                    approved=False,
                    reason="transport_error",
                    prompt_id=decision.prompt_id,
                    execution_generation=decision.execution_generation,
                    command_sha256=decision.command_sha256,
                )
                self._decision = deny
                self._register_settled(deny)
                error = self._try_post(deny)
                if error is not None:
                    self.post_error = error
                self._audit_decision(deny, utterance_text=utterance_text)
                return deny
            self._audit_decision(decision, utterance_text=utterance_text)
            return decision
        except Exception as exc:
            self.post_error = str(exc)
            self._audit_decision(decision, utterance_text=utterance_text)
            return decision
        self._audit_decision(decision, utterance_text=utterance_text)
        return decision

    def decision(self) -> Decision | None:
        """Return the recorded decision, or ``None`` while undecided."""
        return self._decision


__all__ = [
    "READ_ONLY_COMMANDS",
    "ApprovalGate",
    "ApprovalGateError",
    "ApprovalPostError",
    "ApprovalRequest",
    "ControlId",
    "Decision",
    "PlaybackWindow",
    "PromptReadout",
    "Provenance",
    "StaleControlIdError",
    "Utterance",
    "is_read_only_command",
]
