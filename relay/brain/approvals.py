"""Minimal spoken approval gate for destructive tool calls.

The spoken gate is the only approver.  Document text, tool output, and the
model's own agreement can never satisfy an approval.  The gate defaults to
deny, allows one pending prompt per run, and audits every request and decision.
The full policy (prompt_id/generation binding, echo window, confidence floor)
lands in the sibling approvals card; this module is the minimal gate the
prompt-injection tests exercise.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import StrEnum

from relay.logging import (
    audit_approval_decided,
    audit_approval_requested,
    transcript_hash,
)

_NEGATION_TOKENS = frozenset({"no", "not", "dont", "cancel", "stop"})
_PUNCTUATION_RE = re.compile(r"[^a-z0-9\s]")
_APOSTROPHES = ("'", "\u2019")


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


@dataclass(frozen=True)
class ApprovalRequest:
    """One pending approval prompt for a specific tool action."""

    prompt_id: str
    execution_generation: int
    tool: str
    action_summary: str
    required_phrase: str
    timeout_s: float = 20.0


@dataclass(frozen=True)
class Decision:
    """The recorded approval decision; it is final once returned."""

    approved: bool
    reason: str
    prompt_id: str
    execution_generation: int
    actor: str = "spoken_gate"


class ApprovalGateError(Exception):
    """A second prompt was requested while one is still pending."""


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


def _readout(request: ApprovalRequest) -> str:
    return f"{request.action_summary}. Say {request.required_phrase} to confirm."


class ApprovalGate:
    """A one-prompt-at-a-time gate that only accepts spoken confirmation."""

    def __init__(self, *, min_confidence: float = 0.6, audit: bool = True) -> None:
        self.min_confidence = min_confidence
        self.audit = audit
        self._request: ApprovalRequest | None = None
        self._requested_at: float | None = None
        self._decision: Decision | None = None

    @property
    def pending(self) -> ApprovalRequest | None:
        """Return the pending request, or ``None`` once a decision exists."""
        if self._decision is not None:
            return None
        return self._request

    def request(self, request: ApprovalRequest, *, now: float | None = None) -> ApprovalRequest:
        """Record a new prompt; refuse a second prompt while one is pending."""
        pending = self.pending
        if pending is not None:
            msg = f"approval already pending: {pending.prompt_id}"
            raise ApprovalGateError(msg)
        self._request = request
        self._requested_at = time.monotonic() if now is None else now
        self._decision = None
        if self.audit:
            audit_approval_requested(
                prompt_id=request.prompt_id,
                execution_generation=request.execution_generation,
                transcript_sha256=transcript_hash(_readout(request)),
                tool=request.tool,
                summary=request.action_summary,
            )
        return request

    def answer(self, utterance: Utterance, *, now: float | None = None) -> Decision | None:
        """Decide a pending prompt from a spoken utterance, or return ``None``."""
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
            return None
        tokens = _normalize_tokens(utterance.text)
        phrase_tokens = _normalize_tokens(request.required_phrase)
        negation = any(token in _NEGATION_TOKENS for token in tokens)
        phrase_match = _contains_token_sequence(tokens, phrase_tokens)
        if phrase_match and not negation:
            decision = Decision(
                approved=True,
                reason="spoken_phrase",
                prompt_id=request.prompt_id,
                execution_generation=request.execution_generation,
            )
        elif negation:
            decision = Decision(
                approved=False,
                reason="spoken_negation",
                prompt_id=request.prompt_id,
                execution_generation=request.execution_generation,
            )
        else:
            decision = Decision(
                approved=False,
                reason="phrase_mismatch",
                prompt_id=request.prompt_id,
                execution_generation=request.execution_generation,
            )
        self._decision = decision
        if self.audit:
            audit_approval_decided(
                prompt_id=request.prompt_id,
                execution_generation=request.execution_generation,
                decision="approved" if decision.approved else "denied",
                transcript_sha256=transcript_hash(utterance.text),
                actor="spoken_gate",
            )
        return decision

    def expire(self, *, now: float | None = None) -> Decision | None:
        """Deny a pending prompt once its timeout has passed."""
        if self._decision is not None or self._request is None:
            return None
        requested_at = self._requested_at
        if requested_at is None:
            return None
        current = time.monotonic() if now is None else now
        deadline = requested_at + self._request.timeout_s
        if current <= deadline:
            return None
        decision = Decision(
            approved=False,
            reason="timeout",
            prompt_id=self._request.prompt_id,
            execution_generation=self._request.execution_generation,
        )
        self._decision = decision
        if self.audit:
            audit_approval_decided(
                prompt_id=self._request.prompt_id,
                execution_generation=self._request.execution_generation,
                decision="timeout",
                transcript_sha256=None,
                actor="spoken_gate",
            )
        return decision

    def decision(self) -> Decision | None:
        """Return the recorded decision, or ``None`` while undecided."""
        return self._decision


__all__ = [
    "ApprovalGate",
    "ApprovalGateError",
    "ApprovalRequest",
    "Decision",
    "Provenance",
    "Utterance",
]
