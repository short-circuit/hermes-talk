"""SSE decoding and normalized run events for the Hermes run stream.

Threat model: the gateway is a trusted loopback service, but its stream is
still treated as untrusted input.  Frames are parsed incrementally, every
event is validated against the documented envelope, and a per-run gate
enforces ordering, de-duplication and exactly-one terminal event.  Nothing
here performs I/O or touches the network, so the decoder can be unit-tested
without a server.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal


class EventKind(StrEnum):
    """Normalized event names exposed to the relay."""

    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    MESSAGE_INTERIM = "message.interim"
    RUN_COMPLETED = "run.completed"
    RUN_CANCELLED = "run.cancelled"
    RUN_FAILED = "run.failed"
    APPROVAL_REQUEST = "approval.request"
    APPROVAL_SETTLED = "approval.settled"
    CLARIFY_REQUEST = "clarify.request"
    REPLAY_TRUNCATED = "replay.truncated"
    UNKNOWN = "unknown"


TERMINAL_KINDS: frozenset[EventKind] = frozenset(
    {EventKind.RUN_COMPLETED, EventKind.RUN_CANCELLED, EventKind.RUN_FAILED}
)

_EVENT_KIND_BY_NAME: Mapping[str, EventKind] = {
    "tool.started": EventKind.TOOL_STARTED,
    "tool.start": EventKind.TOOL_STARTED,
    "tool.completed": EventKind.TOOL_COMPLETED,
    "tool.complete": EventKind.TOOL_COMPLETED,
    "message.delta": EventKind.MESSAGE_INTERIM,
    "message.interim": EventKind.MESSAGE_INTERIM,
    "run.completed": EventKind.RUN_COMPLETED,
    "run.cancelled": EventKind.RUN_CANCELLED,
    "run.failed": EventKind.RUN_FAILED,
    "approval.request": EventKind.APPROVAL_REQUEST,
    "approval.settled": EventKind.APPROVAL_SETTLED,
    "approval.responded": EventKind.APPROVAL_SETTLED,
    "clarify.request": EventKind.CLARIFY_REQUEST,
    "replay.truncated": EventKind.REPLAY_TRUNCATED,
}

_ENVELOPE_KEYS = frozenset({"event", "seq", "run_id", "timestamp"})


class HermesEventError(ValueError):
    """A frame or event violates the run-stream contract."""


@dataclass(frozen=True)
class SseFrame:
    """One dispatched SSE frame: either event data or a comment."""

    kind: Literal["data", "comment"]
    data: str
    event_id: int | None
    retry_ms: int | None


class SseDecoder:
    """Incremental SSE decoder.

    ``feed`` accepts one line at a time and returns a frame only on dispatch
    (blank line) so callers can stream without buffering the whole response.
    ``flush`` dispatches a trailing frame that was not followed by a blank
    line.  Multi-line ``data`` fields are joined with ``\n`` as the SSE
    specification requires; unknown fields are ignored.
    """

    def __init__(self) -> None:
        self._data: list[str] = []
        self._comment: list[str] = []
        self._event_id: int | None = None
        self._retry_ms: int | None = None

    def feed(self, line: str) -> SseFrame | None:
        """Consume one line and return a frame when the event is complete."""
        line = line.rstrip("\r")
        if not line:
            return self._dispatch()

        if line.startswith(":"):
            comment = line[1:]
            if comment.startswith(" "):
                comment = comment[1:]
            self._comment.append(comment)
            return None

        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]

        if field == "data":
            self._data.append(value)
        elif field == "id":
            self._event_id = _parse_non_negative_int(value)
        elif field == "retry":
            self._retry_ms = _parse_non_negative_int(value)

        return None

    def flush(self) -> SseFrame | None:
        """Dispatch any frame still pending at end of stream."""
        return self._dispatch()

    def _dispatch(self) -> SseFrame | None:
        if self._data:
            frame = SseFrame(
                kind="data",
                data="\n".join(self._data),
                event_id=self._event_id,
                retry_ms=self._retry_ms,
            )
        elif self._comment:
            frame = SseFrame(
                kind="comment",
                data="\n".join(self._comment),
                event_id=self._event_id,
                retry_ms=self._retry_ms,
            )
        else:
            frame = None

        self._data.clear()
        self._comment.clear()
        self._event_id = None
        self._retry_ms = None
        return frame


def _parse_non_negative_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


@dataclass(frozen=True)
class RunEvent:
    """A normalized event from one run's stream."""

    kind: EventKind
    raw_name: str
    seq: int
    run_id: str
    timestamp: float
    payload: Mapping[str, Any]

    @property
    def terminal(self) -> bool:
        return self.kind in TERMINAL_KINDS

    @property
    def tool(self) -> str | None:
        return _str_field(self.payload, "tool")

    @property
    def tool_call_id(self) -> str | None:
        return _str_field(self.payload, "tool_call_id")

    @property
    def text_delta(self) -> str | None:
        delta = _str_field(self.payload, "delta")
        return delta if delta is not None else _str_field(self.payload, "text")

    @property
    def output(self) -> str | None:
        return _str_field(self.payload, "output")

    @property
    def completed(self) -> bool:
        return bool(self.payload.get("completed", False))

    @property
    def partial(self) -> bool:
        return bool(self.payload.get("partial", False))

    @property
    def interrupted(self) -> bool:
        return bool(self.payload.get("interrupted", False))

    @property
    def turn_exit_reason(self) -> str | None:
        return _str_field(self.payload, "turn_exit_reason")

    @property
    def error_message(self) -> str | None:
        return _str_field(self.payload, "error")

    @property
    def prompt_id(self) -> str | None:
        return _str_field(self.payload, "prompt_id")

    @property
    def execution_generation(self) -> int | None:
        value = self.payload.get("execution_generation")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return None


def _str_field(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def decode_event(frame: SseFrame) -> RunEvent:
    """Decode one data frame into a validated :class:`RunEvent`."""
    if frame.kind != "data":
        raise HermesEventError("comment frames are not events")

    try:
        raw = json.loads(frame.data)
    except json.JSONDecodeError as exc:
        raise HermesEventError(f"event data is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise HermesEventError("event data must be a JSON object")

    name = raw.get("event")
    seq = raw.get("seq")
    run_id = raw.get("run_id")
    timestamp = raw.get("timestamp")

    if not isinstance(name, str):
        raise HermesEventError("event name must be a string")
    if not isinstance(seq, int) or isinstance(seq, bool):
        raise HermesEventError("event seq must be an integer")
    if not isinstance(run_id, str):
        raise HermesEventError("event run_id must be a string")
    if (
        not isinstance(timestamp, (int, float))
        or isinstance(timestamp, bool)
        or not math.isfinite(timestamp)
    ):
        raise HermesEventError("event timestamp must be a finite number")

    payload = {key: value for key, value in raw.items() if key not in _ENVELOPE_KEYS}
    return RunEvent(
        kind=_EVENT_KIND_BY_NAME.get(name, EventKind.UNKNOWN),
        raw_name=name,
        seq=seq,
        run_id=run_id,
        timestamp=float(timestamp),
        payload=payload,
    )


class EventGate:
    """Ordering, de-duplication and terminal-once gate for one run."""

    def __init__(self, run_id: str, *, start_after_seq: int | None = None) -> None:
        self._run_id = run_id
        self._last_seq = start_after_seq
        self._terminal_seen = False

    @property
    def last_seq(self) -> int | None:
        return self._last_seq

    @property
    def terminal_seen(self) -> bool:
        return self._terminal_seen

    def observe(self, event: RunEvent) -> bool:
        """Return True when the event should be emitted."""
        if event.run_id != self._run_id:
            raise HermesEventError(
                f"event run_id {event.run_id!r} does not match gate run_id {self._run_id!r}"
            )
        if self._terminal_seen:
            return False
        if self._last_seq is not None and event.seq <= self._last_seq:
            return False

        self._last_seq = event.seq
        if event.terminal:
            self._terminal_seen = True
        return True
