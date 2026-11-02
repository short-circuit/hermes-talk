"""Provenance envelopes for untrusted content and the delegation lifecycle.

Attachments and tool output are handed to the model as data, not as prompt
text: the envelope carries the source kind, a sanitized name, the SHA-256 of
the original bytes or text, and a nonce.  The body is sanitized and truncated
with an explicit marker; path-not-text handoff keeps full documents on disk;
delimiter forgery is neutralised so a hostile body cannot close the envelope
early.

The second half of the module is the delegation lifecycle used by the
``delegate_to_hermes`` tool: :class:`DelegationService` submits one Hermes run
with an idempotency key, returns an acknowledgement without awaiting the
stream, tracks the run id, and delivers exactly one terminal spoken update to
the voice layer (answer, failure, or cancellation).  Both halves are local:
no cloud calls and no speech imports.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from relay.brain.prompts import (
    ENVELOPE_CLOSE_TEMPLATE,
    ENVELOPE_OPEN_TEMPLATE,
    ENVELOPE_PREFIX,
    TRUNCATION_MARKER_TEMPLATE,
    delegation_ack,
    spoken_text,
)
from relay.hermes.client import HermesError, HermesNotFoundError
from relay.hermes.events import EventKind
from relay.storage.inbox import Attachment, InboxStore, sanitize_filename

logger = logging.getLogger(__name__)

MAX_INLINE_EXCERPT_CHARS = 2000
MAX_TOOL_OUTPUT_CHARS = 1200
MAX_UTTERANCE_CHARS = 2000
ALLOWED_SPAN_KINDS = frozenset({"attachment", "document", "tool_output"})
_MAX_SPAN_NAME_CHARS = 64

_BOM = "\ufeff"
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f]")
_NEWLINE_RUN_RE = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class Truncation:
    """One sanitized and possibly truncated untrusted body."""

    text: str
    truncated: bool
    shown_chars: int
    total_chars: int


@dataclass(frozen=True)
class UntrustedSpan:
    """One untrusted span and the provenance metadata attached to it."""

    kind: str
    name: str
    sha256: str
    text: str


def sanitize_untrusted_text(text: str) -> str:
    """Normalise untrusted text without interpreting any of its content."""
    if not isinstance(text, str):
        text = str(text)
    text = text.lstrip(_BOM)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS_RE.sub("", text)
    text = _NEWLINE_RUN_RE.sub("\n\n", text)
    lines = [line.rstrip(" ") for line in text.split("\n")]
    return "\n".join(lines).strip()


def sha256_text(text: str) -> str:
    """Return the hex SHA-256 of text encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def truncate_untrusted(text: str, limit: int) -> Truncation:
    """Sanitize text and truncate it with an explicit, visible marker."""
    if limit <= 0:
        msg = "limit must be positive"
        raise ValueError(msg)
    sanitized = sanitize_untrusted_text(text)
    total = len(sanitized)
    if total <= limit:
        return Truncation(
            text=sanitized,
            truncated=False,
            shown_chars=total,
            total_chars=total,
        )
    kept = sanitized[:limit]
    marker = TRUNCATION_MARKER_TEMPLATE.format(
        shown=f"{limit:,}",
        total=f"{total:,}",
    )
    return Truncation(
        text=f"{kept}\n{marker}",
        truncated=True,
        shown_chars=limit,
        total_chars=total,
    )


def envelope(span: UntrustedSpan, *, limit: int = MAX_INLINE_EXCERPT_CHARS) -> str:
    """Wrap one untrusted span in exactly one opening and one closing tag."""
    if span.kind not in ALLOWED_SPAN_KINDS:
        msg = f"unknown span kind: {span.kind!r}"
        raise ValueError(msg)
    name = sanitize_filename(span.name)[:_MAX_SPAN_NAME_CHARS]
    body = truncate_untrusted(span.text, limit).text
    base_nonce = sha256_text(span.text)[:12]
    nonce = base_nonce
    suffix = 0
    while True:
        open_tag = ENVELOPE_OPEN_TEMPLATE.format(
            kind=span.kind,
            name=name,
            sha256=span.sha256,
            nonce=nonce,
        )
        close_tag = ENVELOPE_CLOSE_TEMPLATE.format(nonce=nonce)
        collision = close_tag in body or open_tag in body
        if not collision and not (suffix == 0 and ENVELOPE_PREFIX in body):
            break
        suffix += 1
        nonce = f"{base_nonce}-{suffix}"
    return f"{open_tag}\n{body}\n{close_tag}"


def attachment_span(attachment: Attachment, excerpt: str | None = None) -> UntrustedSpan:
    """Build the provenance span for one stored attachment."""
    return UntrustedSpan(
        kind="attachment",
        name=sanitize_filename(attachment.original_name),
        sha256=attachment.sha256,
        text=excerpt or "",
    )


def document_span(name: str, text: str) -> UntrustedSpan:
    """Build the provenance span for a document body."""
    return UntrustedSpan(
        kind="document",
        name=name,
        sha256=sha256_text(text),
        text=text,
    )


def tool_output_span(tool: str, text: str) -> UntrustedSpan:
    """Build the provenance span for one tool's raw output."""
    return UntrustedSpan(
        kind="tool_output",
        name=tool,
        sha256=sha256_text(text),
        text=text,
    )


def tool_output_envelope(tool: str, text: str, *, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    """Wrap raw tool output in a bounded provenance envelope."""
    return envelope(tool_output_span(tool, text), limit=limit)


def delegation_input(
    utterance: str,
    attachments: Sequence[Attachment],
    store: InboxStore,
    *,
    excerpts: Mapping[str, str] | None = None,
    inline_images: bool = True,
) -> list[dict[str, object]]:
    """Build the ``input`` content array for ``POST /v1/runs``.

    The user's spoken words come first.  Every attachment then contributes the
    trusted machine line from :meth:`InboxStore.delegation_parts` (plus its
    inline image when applicable), and only an explicit excerpt is wrapped in
    an untrusted envelope.  A document without an excerpt is handed over as a
    path only.
    """
    parts: list[dict[str, object]] = [
        {
            "type": "text",
            "text": truncate_untrusted(utterance, MAX_UTTERANCE_CHARS).text,
        }
    ]
    for attachment in attachments:
        parts.extend(store.delegation_parts([attachment], inline_images=inline_images))
        if excerpts is not None and attachment.id in excerpts:
            parts.append(
                {
                    "type": "text",
                    "text": envelope(attachment_span(attachment, excerpts[attachment.id])),
                }
            )
    return parts


# --- delegation lifecycle -------------------------------------------------
#
# The voice brain enqueues one Hermes run and returns immediately with an
# acknowledgement.  A background watcher owns the stream, records the run id,
# and funnels every terminal path through one delivery method so the voice
# layer hears the final answer exactly once.  Idempotency keys are checked
# under a per-key lock before submission, and the registry is bounded.

MAX_INSTRUCTION_CHARS = MAX_UTTERANCE_CHARS
MAX_IDEMPOTENCY_KEY_CHARS = 128
MAX_RECORDS = 256
RECORD_TTL_S = 3600.0

_MAX_FAILURE_CHARS = 200
_HOME_PREFIX_RE = re.compile(r"(?<![\w.])/(?:home|Users)/[^/\s]+")
_SECRET_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|"
    r"github_pat_[A-Za-z0-9_]{40,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"AKIA[A-Za-z0-9]{16}|AIza[A-Za-z0-9_-]{35}|hf_[A-Za-z0-9]{30,}|"
    r"glpat-[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
_REDACTED = "[redacted]"


class DelegationError(RuntimeError):
    """Base class for delegation lifecycle failures."""


class DelegationConfigError(DelegationError):
    """The caller supplied an unusable delegation argument."""


class DelegationStateError(DelegationError):
    """The service observed an invalid internal state transition."""


class DelegationState(StrEnum):
    """Lifecycle states tracked for one delegation."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class SpokenUpdate:
    """One terminal spoken update for the voice layer."""

    delegation_id: str
    run_id: str | None
    kind: Literal["answer", "failure", "cancelled"]
    text: str
    final: bool


@dataclass(frozen=True)
class DelegationHandle:
    """The immediate acknowledgement returned to the caller."""

    delegation_id: str
    idempotency_key: str
    run_id: str | None
    state: DelegationState
    spoken_ack: str
    deduplicated: bool
    replayed: bool
    enqueue_ms: float
    failure: str | None


@dataclass(frozen=True)
class DelegationSnapshot:
    """A frozen view of one delegation record."""

    delegation_id: str
    idempotency_key: str
    run_id: str | None
    state: DelegationState
    created_at: float
    terminal: bool
    enqueue_ms: float
    deduplicated: bool
    replayed: bool
    failure: str | None
    delivery_error: str | None
    deliveries: int


DELEGATION_FAILURE_PHRASES: tuple[str, ...] = ("Sorry, that task didn't finish.",)
DELEGATION_CANCELLED_PHRASES: tuple[str, ...] = ("Stopped.",)


def delegation_failure() -> str:
    """Return the deterministic spoken failure phrase."""
    return DELEGATION_FAILURE_PHRASES[0]


def delegation_cancelled() -> str:
    """Return the deterministic spoken cancellation phrase."""
    return DELEGATION_CANCELLED_PHRASES[0]


@dataclass
class _DelegationRecord:
    """Mutable internal state for one tracked delegation."""

    delegation_id: str
    idempotency_key: str
    run_id: str | None
    state: DelegationState
    created_at: float
    enqueue_ms: float
    deduplicated: bool = False
    replayed: bool = False
    failure: str | None = None
    delivery_error: str | None = None
    deliveries: int = 0
    delivered: bool = False
    completion: asyncio.Event = field(default_factory=asyncio.Event)
    watcher: asyncio.Task[None] | None = None


def _validated_instruction(instruction: object) -> str:
    if not isinstance(instruction, str):
        msg = "instruction must be a string"
        raise DelegationConfigError(msg)
    text = truncate_untrusted(instruction, MAX_INSTRUCTION_CHARS).text
    if not text:
        msg = "instruction is empty after sanitizing"
        raise DelegationConfigError(msg)
    return text


def _validated_key(value: object) -> str:
    if not isinstance(value, str):
        msg = "idempotency key must be a string"
        raise DelegationConfigError(msg)
    if not 1 <= len(value) <= MAX_IDEMPOTENCY_KEY_CHARS:
        msg = f"idempotency key must be 1..{MAX_IDEMPOTENCY_KEY_CHARS} characters"
        raise DelegationConfigError(msg)
    if any(not 0x20 <= ord(character) <= 0x7E for character in value):
        msg = "idempotency key must contain only printable ASCII characters"
        raise DelegationConfigError(msg)
    return value


def _redact_sensitive(text: str) -> str:
    text = _HOME_PREFIX_RE.sub("~", text)
    return _SECRET_RE.sub(_REDACTED, text)


def _bounded_failure(value: str | None, fallback: str) -> str:
    text = sanitize_untrusted_text(value) if isinstance(value, str) else ""
    text = _redact_sensitive(text)
    if not text:
        text = fallback
    return text[:_MAX_FAILURE_CHARS]


def _exception_failure(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return _bounded_failure(code, type(exc).__name__)
    return type(exc).__name__


class DelegationService:
    """Enqueue Hermes runs and deliver exactly one terminal spoken update."""

    def __init__(
        self,
        client: object,
        deliver: Callable[[SpokenUpdate], Awaitable[None]],
        *,
        session_id: str | None = None,
        session_key: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_records: int = MAX_RECORDS,
        record_ttl_s: float = RECORD_TTL_S,
        key_factory: Callable[[], str] | None = None,
    ) -> None:
        if max_records < 0:
            msg = "max_records must be zero or greater"
            raise DelegationConfigError(msg)
        self._client = client
        self._deliver_fn = deliver
        self._session_id = session_id
        self._session_key = session_key
        self._clock = clock
        self._max_records = max_records
        self._record_ttl_s = record_ttl_s
        self._key_factory = key_factory
        self._records: dict[str, _DelegationRecord] = {}
        self._records_by_key: dict[str, str] = {}
        self._records_by_run: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._watchers: set[asyncio.Task[None]] = set()
        self._delivery_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    async def delegate(
        self,
        instruction: str,
        *,
        idempotency_key: str | None = None,
        session_id: str | None = None,
        session_key: str | None = None,
    ) -> DelegationHandle:
        """Submit one instruction and return an acknowledgement immediately."""
        started = time.perf_counter()
        text = _validated_instruction(instruction)
        if idempotency_key is None:
            if self._key_factory is not None:
                key = _validated_key(self._key_factory())
            else:
                key = _validated_key(f"dlg-{uuid.uuid4().hex}")
        else:
            key = _validated_key(idempotency_key)

        effective_session_id = session_id if session_id is not None else self._session_id
        effective_session_key = session_key if session_key is not None else self._session_key

        lock = self._lock_for(key)
        async with lock:
            self._prune()
            existing_id = self._records_by_key.get(key)
            if existing_id is not None:
                existing = self._records.get(existing_id)
                if existing is not None:
                    existing.deduplicated = True
                    logger.info(
                        "delegation %s run %s deduplicated state %s",
                        existing.delegation_id,
                        existing.run_id,
                        existing.state.value,
                    )
                    return self._handle(existing)

            record = _DelegationRecord(
                delegation_id=f"dlg-{uuid.uuid4().hex}",
                idempotency_key=key,
                run_id=None,
                state=DelegationState.RUNNING,
                created_at=self._clock(),
                enqueue_ms=0.0,
            )
            self._register(record)
            self._prune()

            try:
                admission = await self._client.submit_run(
                    text,
                    session_id=effective_session_id,
                    session_key=effective_session_key,
                    idempotency_key=key,
                )
            except HermesError as exc:
                record.state = DelegationState.FAILED
                record.failure = _exception_failure(exc)
                record.enqueue_ms = (time.perf_counter() - started) * 1000.0
                logger.warning(
                    "delegation %s submit failed (%s)",
                    record.delegation_id,
                    type(exc).__name__,
                )
                self._schedule_delivery(
                    record,
                    DelegationState.FAILED,
                    "failure",
                    delegation_failure(),
                    failure=record.failure,
                )
                return self._handle(record)

            record.run_id = admission.run_id
            record.replayed = bool(admission.replayed)
            record.enqueue_ms = (time.perf_counter() - started) * 1000.0
            self._records_by_run[admission.run_id] = record.delegation_id
            record.watcher = asyncio.create_task(self._watch(record))
            self._watchers.add(record.watcher)
            record.watcher.add_done_callback(self._watchers.discard)
            logger.info(
                "delegation %s run %s submitted state %s",
                record.delegation_id,
                record.run_id,
                record.state.value,
            )
            return self._handle(record)

    async def stop(self, delegation_id: str) -> DelegationHandle:
        """Ask the gateway to stop a running delegation, once."""
        record = self._records.get(delegation_id)
        if record is None:
            raise KeyError(delegation_id)
        if record.state is not DelegationState.RUNNING or record.run_id is None:
            return self._handle(record)

        try:
            await self._client.stop(record.run_id)
        except HermesNotFoundError:
            await self._finish(
                record,
                DelegationState.CANCELLED,
                "cancelled",
                delegation_cancelled(),
            )
        except HermesError as exc:
            logger.warning(
                "delegation %s stop failed (%s)",
                record.delegation_id,
                type(exc).__name__,
            )
        else:
            await self._finish(
                record,
                DelegationState.CANCELLED,
                "cancelled",
                delegation_cancelled(),
            )
        return self._handle(record)

    async def wait(
        self,
        delegation_id: str,
        *,
        timeout_s: float | None = None,
    ) -> DelegationSnapshot:
        """Wait for the terminal state of one delegation."""
        record = self._records.get(delegation_id)
        if record is None:
            raise KeyError(delegation_id)
        if timeout_s is None:
            await record.completion.wait()
        else:
            await asyncio.wait_for(record.completion.wait(), timeout=timeout_s)
        return self._snapshot(record)

    def snapshot(self, delegation_id: str) -> DelegationSnapshot:
        """Return a frozen snapshot; unknown ids raise ``KeyError``."""
        record = self._records.get(delegation_id)
        if record is None:
            raise KeyError(delegation_id)
        return self._snapshot(record)

    def by_run_id(self, run_id: str) -> DelegationSnapshot | None:
        """Return the delegation tracking this run id, if any."""
        delegation_id = self._records_by_run.get(run_id)
        if delegation_id is None:
            return None
        record = self._records.get(delegation_id)
        if record is None:
            return None
        return self._snapshot(record)

    def by_idempotency_key(self, key: str) -> DelegationSnapshot | None:
        """Return the delegation registered for this idempotency key, if any."""
        delegation_id = self._records_by_key.get(key)
        if delegation_id is None:
            return None
        record = self._records.get(delegation_id)
        if record is None:
            return None
        return self._snapshot(record)

    async def aclose(self) -> None:
        """Cancel and await every watcher and pending delivery task."""
        self._closed = True
        pending = [task for task in (*self._watchers, *self._delivery_tasks) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._watchers.clear()
        self._delivery_tasks.clear()
        for record in self._records.values():
            record.completion.set()

    async def _watch(self, record: _DelegationRecord) -> None:
        run_id = record.run_id
        if run_id is None:
            return
        stream: object | None = None
        try:
            stream = self._client.events(run_id)
            async for event in stream:
                if event.kind is EventKind.RUN_COMPLETED:
                    await self._finish(
                        record,
                        DelegationState.COMPLETED,
                        "answer",
                        self._answer_text(event.output),
                    )
                    return
                if event.kind is EventKind.RUN_FAILED:
                    await self._finish(
                        record,
                        DelegationState.FAILED,
                        "failure",
                        delegation_failure(),
                        failure=_bounded_failure(event.error_message, "run.failed"),
                    )
                    return
                if event.kind is EventKind.RUN_CANCELLED:
                    await self._finish(
                        record,
                        DelegationState.CANCELLED,
                        "cancelled",
                        delegation_cancelled(),
                    )
                    return
            await self._finish(
                record,
                DelegationState.FAILED,
                "failure",
                delegation_failure(),
                failure="stream.ended",
            )
        except asyncio.CancelledError:
            raise
        except HermesError as exc:
            logger.warning(
                "delegation %s run %s stream failed (%s)",
                record.delegation_id,
                run_id,
                type(exc).__name__,
            )
            await self._finish(
                record,
                DelegationState.FAILED,
                "failure",
                delegation_failure(),
                failure=type(exc).__name__,
            )
        except Exception as exc:
            logger.warning(
                "delegation %s run %s stream failed (%s)",
                record.delegation_id,
                run_id,
                type(exc).__name__,
            )
            await self._finish(
                record,
                DelegationState.FAILED,
                "failure",
                delegation_failure(),
                failure=type(exc).__name__,
            )
        finally:
            if stream is not None:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.debug(
                            "delegation %s stream close failed (%s)",
                            record.delegation_id,
                            type(exc).__name__,
                        )

    async def _finish(
        self,
        record: _DelegationRecord,
        state: DelegationState,
        kind: Literal["answer", "failure", "cancelled"],
        text: str,
        *,
        failure: str | None = None,
    ) -> None:
        if record.delivered:
            return
        record.delivered = True
        record.state = state
        record.failure = failure
        logger.info(
            "delegation %s run %s state %s",
            record.delegation_id,
            record.run_id,
            record.state.value,
        )
        if self._closed:
            record.completion.set()
            return
        record.deliveries = 1
        update = SpokenUpdate(
            delegation_id=record.delegation_id,
            run_id=record.run_id,
            kind=kind,
            text=text,
            final=True,
        )
        try:
            await self._deliver_fn(update)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record.delivery_error = type(exc).__name__
            logger.warning(
                "delegation %s delivery failed (%s)",
                record.delegation_id,
                type(exc).__name__,
            )
        finally:
            record.completion.set()

    def _schedule_delivery(
        self,
        record: _DelegationRecord,
        state: DelegationState,
        kind: Literal["answer", "failure", "cancelled"],
        text: str,
        *,
        failure: str | None = None,
    ) -> None:
        task = asyncio.create_task(self._finish(record, state, kind, text, failure=failure))
        self._delivery_tasks.add(task)
        task.add_done_callback(self._delivery_tasks.discard)

    def _answer_text(self, output: str | None) -> str:
        if isinstance(output, str) and output:
            text = _HOME_PREFIX_RE.sub("~", spoken_text(_SECRET_RE.sub(_REDACTED, output)))
            if text:
                return text
        # Imported lazily: narration imports this module for truncation.
        from relay.brain.narration import narrate_run_completed

        return narrate_run_completed()

    def _handle(self, record: _DelegationRecord) -> DelegationHandle:
        return DelegationHandle(
            delegation_id=record.delegation_id,
            idempotency_key=record.idempotency_key,
            run_id=record.run_id,
            state=record.state,
            spoken_ack=delegation_ack(),
            deduplicated=record.deduplicated,
            replayed=record.replayed,
            enqueue_ms=record.enqueue_ms,
            failure=record.failure,
        )

    def _snapshot(self, record: _DelegationRecord) -> DelegationSnapshot:
        return DelegationSnapshot(
            delegation_id=record.delegation_id,
            idempotency_key=record.idempotency_key,
            run_id=record.run_id,
            state=record.state,
            created_at=record.created_at,
            terminal=record.state is not DelegationState.RUNNING,
            enqueue_ms=record.enqueue_ms,
            deduplicated=record.deduplicated,
            replayed=record.replayed,
            failure=record.failure,
            delivery_error=record.delivery_error,
            deliveries=record.deliveries,
        )

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _register(self, record: _DelegationRecord) -> None:
        self._records[record.delegation_id] = record
        self._records_by_key[record.idempotency_key] = record.delegation_id

    def _remove(self, record: _DelegationRecord) -> None:
        self._records.pop(record.delegation_id, None)
        if self._records_by_key.get(record.idempotency_key) == record.delegation_id:
            self._records_by_key.pop(record.idempotency_key, None)
        if (
            record.run_id is not None
            and self._records_by_run.get(record.run_id) == record.delegation_id
        ):
            self._records_by_run.pop(record.run_id, None)

    def _prune(self) -> None:
        now = self._clock()
        for record in list(self._records.values()):
            if record.state is DelegationState.RUNNING:
                continue
            if now - record.created_at > self._record_ttl_s:
                self._remove(record)
        while len(self._records) > self._max_records:
            terminal = [
                record
                for record in self._records.values()
                if record.state is not DelegationState.RUNNING
            ]
            if not terminal:
                break
            oldest = min(terminal, key=lambda record: record.created_at)
            self._remove(oldest)


__all__ = [
    "ALLOWED_SPAN_KINDS",
    "DELEGATION_CANCELLED_PHRASES",
    "DELEGATION_FAILURE_PHRASES",
    "MAX_IDEMPOTENCY_KEY_CHARS",
    "MAX_INLINE_EXCERPT_CHARS",
    "MAX_INSTRUCTION_CHARS",
    "MAX_RECORDS",
    "MAX_TOOL_OUTPUT_CHARS",
    "MAX_UTTERANCE_CHARS",
    "RECORD_TTL_S",
    "DelegationConfigError",
    "DelegationError",
    "DelegationHandle",
    "DelegationService",
    "DelegationSnapshot",
    "DelegationState",
    "DelegationStateError",
    "SpokenUpdate",
    "Truncation",
    "UntrustedSpan",
    "attachment_span",
    "delegation_cancelled",
    "delegation_failure",
    "delegation_input",
    "document_span",
    "envelope",
    "sanitize_untrusted_text",
    "sha256_text",
    "tool_output_envelope",
    "tool_output_span",
    "truncate_untrusted",
]
