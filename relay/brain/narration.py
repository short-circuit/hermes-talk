"""Narration reads mapped phrases, never raw tool output.

Tool output is untrusted data: it is sanitized and truncated with an explicit
marker before it is spoken.  Narration can never approve anything; approval is
the spoken gate's job.  No network calls are made here.

The stateful :class:`ToolNarrator` consumes normalized Hermes run events and
emits at most one redacted phrase per throttle window.  It only reads the tool
name from ``tool.started`` events; ``tool.completed`` and every output field
are ignored so raw tool output can never reach the speaker.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from relay.brain.delegation import truncate_untrusted
from relay.brain.prompts import spoken_text
from relay.hermes.events import TERMINAL_KINDS, EventKind, RunEvent
from relay.logging.redact import scrub

MAX_NARRATION_CHARS = 160
MIN_NARRATION_INTERVAL_S: float = 4.0
_FALLBACK_NARRATION = "Working."
_REDACTION_MASK = "***"
_MASK_PLACEHOLDER = "\x00MASK\x00"
_HOME_PLACEHOLDER = "\x00HOME\x00"
TOOL_PHRASES: Mapping[str, str] = {
    "read_file": "Reading the file.",
    "write_file": "Writing the file.",
    "edit_file": "Editing the file.",
    "terminal": "Running a command.",
    "web_search": "Searching the web.",
    "web_fetch": "Fetching the page.",
    "delegate_task": "Delegating the task.",
    "kanban_complete": "Completing the card.",
    "approval": "Waiting for approval.",
    "default": "Working on it.",
}
DEFAULT_TOOL_PHRASE = "Working on it."


def _bound_at_word(text: str, max_chars: int) -> str:
    """Bound text at a word boundary without ever exceeding ``max_chars``."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    cut = window.rfind(" ")
    if cut <= 0:
        return window
    return f"{window[:cut].rstrip()}\u2026"


def redact_spoken(text: str, *, max_chars: int = MAX_NARRATION_CHARS) -> str:
    """Redact, clean, and bound text before it reaches the speaker.

    Every spoken string passes through :func:`relay.logging.redact.scrub`, so
    registered secrets, bearer tokens, key/value pairs, and home-directory
    paths cannot leak even when a caller supplies its own phrase map.
    """
    try:
        # Scrub first so registered secrets are matched before markdown
        # cleanup can remove characters inside them.  The mask and the home
        # marker are protected across spoken_text(), which strips ``*``/``~``.
        masked = scrub(text)
        protected = masked.replace(_REDACTION_MASK, _MASK_PLACEHOLDER).replace(
            "~", _HOME_PLACEHOLDER
        )
        cleaned = spoken_text(protected)
        restored = cleaned.replace(_MASK_PLACEHOLDER, _REDACTION_MASK).replace(
            _HOME_PLACEHOLDER, "~"
        )
        return _bound_at_word(restored, max_chars)
    except Exception:
        try:
            return _bound_at_word(str(text), max_chars)
        except Exception:
            return _bound_at_word(_FALLBACK_NARRATION, max_chars)


def phrase_for_tool(tool: str) -> str:
    """Return the fixed phrase for a tool; never echo the raw tool name."""
    return TOOL_PHRASES.get(tool, DEFAULT_TOOL_PHRASE)


def narrate_tool_started(tool: str) -> str:
    """Return the redacted mapped phrase for a tool that just started."""
    return redact_spoken(phrase_for_tool(tool))


def narrate_tool_completed(tool: str, output: str) -> str:
    """Return the redacted mapped phrase plus a bounded, visibly marked excerpt."""
    phrase = redact_spoken(phrase_for_tool(tool))
    excerpt = scrub(truncate_untrusted(output, MAX_NARRATION_CHARS).text)
    if not excerpt:
        return phrase
    return f"{phrase} {excerpt}"


def narrate_run_completed() -> str:
    """Return the redacted fixed completion phrase."""
    return scrub("Done.")


@dataclass(frozen=True)
class NarrationUpdate:
    """One narration emitted for an observed tool start.

    ``emit_at`` and ``observed_at`` are the same synchronous clock reading, so
    ``latency_s`` is zero and the update can be spoken immediately.
    """

    text: str
    tool: str | None
    run_id: str
    emit_at: float
    observed_at: float
    suppressed_since_last: int

    @property
    def latency_s(self) -> float:
        """Return the measured emission latency in seconds."""
        return self.emit_at - self.observed_at


@dataclass(frozen=True)
class NarrationSnapshot:
    """Cumulative counters and last emission for one narrator instance."""

    run_id: str | None
    emitted: int
    suppressed: int
    last_emit_at: float | None
    last_text: str | None


class ToolNarrator:
    """Stateful, throttled narrator for synthetic Hermes run events.

    A lock guards every state mutation.  ``observe`` is synchronous: it performs
    no awaits, sleeps, I/O, or raw-output logging, so narration latency stays at
    zero.  Only ``tool.started`` can produce speech; terminal events clear the
    throttle baseline so a subsequent run narrates immediately.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        min_interval_s: float = MIN_NARRATION_INTERVAL_S,
        phrases: Mapping[str, str] | None = None,
        run_id: str | None = None,
    ) -> None:
        if min_interval_s <= 0:
            msg = "min_interval_s must be positive"
            raise ValueError(msg)
        self._clock = clock
        self._min_interval_s = float(min_interval_s)
        self._phrases: dict[str, str] = dict(TOOL_PHRASES if phrases is None else phrases)
        self._run_id = run_id
        self._lock = threading.Lock()
        self._emitted = 0
        self._suppressed = 0
        self._last_emit_at: float | None = None
        self._last_text: str | None = None
        self._suppressed_since_last = 0

    def observe(self, event: RunEvent) -> NarrationUpdate | None:
        """Return one narration update when a tool start is allowed to speak."""
        with self._lock:
            if self._run_id is not None and event.run_id != self._run_id:
                return None
            if event.kind in TERMINAL_KINDS:
                # Rebase the throttle: the next run may narrate immediately.
                self._last_emit_at = None
                return None
            if event.kind is not EventKind.TOOL_STARTED:
                return None

            now = self._clock()
            last = self._last_emit_at
            if last is not None and now >= last and now - last < self._min_interval_s:
                self._suppressed += 1
                self._suppressed_since_last += 1
                return None
            # A backwards clock is treated as an allowed emission and rebased
            # below, so a non-monotonic clock can never wedge the narrator.

            phrase = self._phrases.get(event.tool or "", DEFAULT_TOOL_PHRASE)
            text = redact_spoken(phrase)
            if not text:
                text = redact_spoken(DEFAULT_TOOL_PHRASE)
            if not text:
                text = _FALLBACK_NARRATION

            suppressed_since_last = self._suppressed_since_last
            self._suppressed_since_last = 0
            self._last_emit_at = now
            self._last_text = text
            self._emitted += 1
            return NarrationUpdate(
                text=text,
                tool=event.tool,
                run_id=event.run_id,
                emit_at=now,
                observed_at=now,
                suppressed_since_last=suppressed_since_last,
            )

    def reset(self) -> None:
        """Clear throttle state and counters while keeping configuration."""
        with self._lock:
            self._emitted = 0
            self._suppressed = 0
            self._last_emit_at = None
            self._last_text = None
            self._suppressed_since_last = 0

    def snapshot(self) -> NarrationSnapshot:
        """Return cumulative counters and the most recent emission."""
        with self._lock:
            return NarrationSnapshot(
                run_id=self._run_id,
                emitted=self._emitted,
                suppressed=self._suppressed,
                last_emit_at=self._last_emit_at,
                last_text=self._last_text,
            )


__all__ = [
    "DEFAULT_TOOL_PHRASE",
    "MAX_NARRATION_CHARS",
    "MIN_NARRATION_INTERVAL_S",
    "TOOL_PHRASES",
    "NarrationSnapshot",
    "NarrationUpdate",
    "ToolNarrator",
    "narrate_run_completed",
    "narrate_tool_completed",
    "narrate_tool_started",
    "phrase_for_tool",
    "redact_spoken",
]
