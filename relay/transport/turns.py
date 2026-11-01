"""Turn boundaries, local voice-activity detection, and barge-in for the relay.

This module owns when a user utterance starts and ends, when the relay commits
it, when a reply is speaking, and how an interruption is confirmed.  No audio
leaves the process: microphone PCM is inspected locally, and every decision is
deterministic.  The VAD is energy-based and needs no model file; the
``VAD_MODEL`` setting is advisory, and a silero VAD can later be swapped in
behind :class:`VadEngine` without changing callers.

Nothing here contacts the network except the explicitly injected
:class:`RunStopper`.  ``httpx`` is imported lazily, only when an
:class:`HttpRunStopper` is actually asked to stop a run.

:meth:`TurnController.feed_mic` and the rest of the controller are designed for
a single event loop and are not thread-safe.
"""

from __future__ import annotations

import enum
import inspect
import logging
import math
import re
import secrets
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlsplit

from relay.limits import BargeInGate
from relay.speech.audio import PcmChunk, _import_numpy
from relay.speech.stt import TranscriptEvent

_LOGGER = logging.getLogger(__name__)

DEFAULT_FLUSH_BUDGET_MS = 300.0
MAX_SPOKEN_TAIL_CHARS = 80
INTERRUPTION_MARKER_TEMPLATE = (
    "[relay: your previous reply was interrupted by the user after {speech_ms} ms of speech{tail}. "
    "It was cut off mid-reply. Do not repeat what you already said; continue from the cut-off point.]"
)

_MAX_EVENTS = 200
_SILENCE_DBFS = -90.0
_BYTES_PER_SAMPLE = 2
_FULL_SCALE = 32768.0
_MILLISECONDS_PER_SECOND = 1000.0
_TERMINAL_PUNCTUATION = frozenset({".", "!", "?", "…"})
_DEFAULT_TRAILING_FILLERS = frozenset(
    {
        "um",
        "uh",
        "er",
        "hmm",
        "like",
        "so",
        "and",
        "but",
        "or",
        "because",
        "then",
        "which",
        "that",
        "if",
        "when",
        "while",
        "to",
        "of",
        "with",
    }
)
_QUOTE_CHARS = frozenset({'"', "'", "\u201c", "\u201d", "\u2018", "\u2019", "`"})
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class TurnError(Exception):
    """Base class for turn and interruption failures."""


class TurnStateError(TurnError):
    """A turn was asked to move to a state it cannot reach."""


class TurnConfigError(TurnError, ValueError):
    """A turn, VAD, or interruption component was configured incorrectly."""


def _normalize_text(text: str) -> str:
    return " ".join(text.split()).strip()


def _sanitize_spoken(text: str) -> str:
    """Collapse whitespace and remove quotes and control characters."""
    cleaned = "".join(char for char in text if char not in _QUOTE_CHARS)
    cleaned = _CONTROL_RE.sub(" ", cleaned)
    return _normalize_text(cleaned)


def _last_word(text: str) -> str:
    words = text.split()
    if not words:
        return ""
    return words[-1].strip(".,!?…;:").lower()


def _word_count(text: str) -> int:
    return len(text.split())


class VadState(enum.Enum):
    """The energy VAD's state for the current utterance."""

    SILENCE = "silence"
    CANDIDATE = "candidate"
    SPEECH = "speech"
    HANGOVER = "hangover"


@dataclass(frozen=True)
class VadFrame:
    """One VAD decision for one audio sub-frame."""

    db: float
    state: VadState
    is_voiced: bool
    onset: bool
    offset: bool
    speech_ms: int
    silence_ms: int
    speech_started_at: float | None
    frame_index: int


@dataclass(frozen=True)
class VadConfig:
    """Energy VAD tuning; all values are local and model-free."""

    frame_ms: int = 20
    sample_rate: int = 16000
    absolute_speech_dbfs: float = -40.0
    noise_margin_db: float = 12.0
    noise_floor_alpha: float = 0.05
    onset_ms: int = 60
    hangover_ms: int = 300
    max_frame_ms: int = 500

    def __post_init__(self) -> None:
        if self.frame_ms <= 0:
            msg = "VadConfig.frame_ms must be greater than zero"
            raise TurnConfigError(msg)
        if self.sample_rate <= 0:
            msg = "VadConfig.sample_rate must be greater than zero"
            raise TurnConfigError(msg)
        if not math.isfinite(self.absolute_speech_dbfs):
            msg = "VadConfig.absolute_speech_dbfs must be finite"
            raise TurnConfigError(msg)
        if not math.isfinite(self.noise_margin_db) or self.noise_margin_db < 0:
            msg = "VadConfig.noise_margin_db must be finite and zero or greater"
            raise TurnConfigError(msg)
        if not 0 < self.noise_floor_alpha <= 1:
            msg = "VadConfig.noise_floor_alpha must be between zero and one"
            raise TurnConfigError(msg)
        if self.onset_ms < 1:
            msg = "VadConfig.onset_ms must be at least one"
            raise TurnConfigError(msg)
        if self.hangover_ms < 0:
            msg = "VadConfig.hangover_ms must be zero or greater"
            raise TurnConfigError(msg)
        if self.max_frame_ms < self.frame_ms:
            msg = "VadConfig.max_frame_ms must be at least VadConfig.frame_ms"
            raise TurnConfigError(msg)


class VadEngine(Protocol):
    """A local voice-activity detector."""

    def process(self, chunk: PcmChunk) -> tuple[VadFrame, ...]: ...

    def reset(self) -> None: ...

    def snapshot(self) -> dict[str, object]: ...


class EnergyVad:
    """Model-free energy VAD with an adaptive noise floor.

    Loudness is RMS over int16 samples in dBFS.  The noise floor decays
    quickly toward quieter frames and rises slowly toward louder ones, so a
    steady hum eventually stops registering as speech.  Frames are processed in
    order and the state machine is deterministic.
    """

    def __init__(self, config: VadConfig, *, clock: Callable[[], float] = time.monotonic) -> None:
        if not callable(clock):
            msg = "EnergyVad clock must be callable"
            raise TurnConfigError(msg)
        self._config = config
        self._clock = clock
        self._frame_samples = int(config.sample_rate * config.frame_ms / 1000)
        if self._frame_samples < 1:
            msg = "VadConfig.frame_ms produces an empty frame"
            raise TurnConfigError(msg)
        self._frame_ms = config.frame_ms
        self._noise_floor_db = _SILENCE_DBFS
        self._reset_state()
        self._frames = 0
        self._onsets = 0
        self._offsets = 0
        self._voiced_frames = 0

    def _reset_state(self) -> None:
        self._state = VadState.SILENCE
        self._candidate_ms = 0
        self._speech_ms = 0
        self._silence_ms = 0
        self._speech_started_at: float | None = None
        self._frame_index = 0

    def reset(self) -> None:
        """Reset the state machine and counters, including the noise floor."""
        self._noise_floor_db = _SILENCE_DBFS
        self._reset_state()
        self._frames = 0
        self._onsets = 0
        self._offsets = 0
        self._voiced_frames = 0

    def snapshot(self) -> dict[str, object]:
        """Return counters only; never audio bytes."""
        return {
            "frames": self._frames,
            "onsets": self._onsets,
            "offsets": self._offsets,
            "voiced_frames": self._voiced_frames,
            "noise_floor_db": round(self._noise_floor_db, 3),
            "state": self._state.value,
        }

    def _loudness_db(self, data: bytes) -> float:
        numpy = _import_numpy()
        samples = numpy.frombuffer(data, dtype=numpy.int16).astype(numpy.float64)
        if samples.size == 0:
            return _SILENCE_DBFS
        rms = float(numpy.sqrt(numpy.mean(numpy.square(samples))))
        if rms <= 0.0:
            return _SILENCE_DBFS
        return max(_SILENCE_DBFS, 20.0 * math.log10(rms / _FULL_SCALE))

    def _threshold_db(self) -> float:
        return max(
            self._config.absolute_speech_dbfs, self._noise_floor_db + self._config.noise_margin_db
        )

    def _adapt_noise_floor(self, db: float) -> None:
        alpha = self._config.noise_floor_alpha
        if db < self._noise_floor_db:
            self._noise_floor_db += alpha * (db - self._noise_floor_db)
        else:
            self._noise_floor_db += alpha * 0.1 * (db - self._noise_floor_db)
        self._noise_floor_db = max(_SILENCE_DBFS, self._noise_floor_db)

    def _frame_duration_ms(self, samples: int) -> int:
        return max(1, round(samples * _MILLISECONDS_PER_SECOND / self._config.sample_rate))

    def _advance(
        self,
        *,
        db: float,
        voiced: bool,
        duration_ms: int,
        frame_index: int,
    ) -> VadFrame:
        onset = False
        offset = False
        if self._state is VadState.SILENCE:
            if voiced:
                self._state = VadState.CANDIDATE
                self._candidate_ms = duration_ms
                self._speech_started_at = self._clock()
            else:
                self._candidate_ms = 0
                self._speech_ms = 0
                self._silence_ms = 0
        elif self._state is VadState.CANDIDATE:
            if voiced:
                self._candidate_ms += duration_ms
                if self._candidate_ms >= self._config.onset_ms:
                    self._state = VadState.SPEECH
                    self._speech_ms = self._candidate_ms
                    self._silence_ms = 0
                    onset = True
                    self._onsets += 1
            else:
                self._state = VadState.SILENCE
                self._candidate_ms = 0
                self._speech_ms = 0
                self._silence_ms = 0
                self._speech_started_at = None
        elif self._state is VadState.SPEECH:
            if voiced:
                self._speech_ms += duration_ms
                self._silence_ms = 0
            else:
                self._state = VadState.HANGOVER
                self._silence_ms = duration_ms
        elif self._state is VadState.HANGOVER:
            if voiced:
                self._state = VadState.SPEECH
                self._speech_ms += duration_ms
                self._silence_ms = 0
            else:
                self._silence_ms += duration_ms
                if self._silence_ms >= self._config.hangover_ms:
                    offset = True
                    self._offsets += 1
                    self._state = VadState.SILENCE
                    self._candidate_ms = 0
                    self._speech_started_at = None

        frame = VadFrame(
            db=db,
            state=self._state,
            is_voiced=voiced,
            onset=onset,
            offset=offset,
            speech_ms=self._speech_ms,
            silence_ms=self._silence_ms,
            speech_started_at=self._speech_started_at,
            frame_index=frame_index,
        )
        if offset:
            self._speech_ms = 0
            self._silence_ms = 0
        return frame

    def process(self, chunk: PcmChunk) -> tuple[VadFrame, ...]:
        """Validate, split, and classify one PCM chunk into VAD sub-frames."""
        chunk.validate()
        if chunk.sample_rate != self._config.sample_rate:
            msg = (
                f"VAD requires {self._config.sample_rate} Hz mono PCM16, "
                f"got {chunk.sample_rate} Hz; resample before calling"
            )
            raise ValueError(msg)
        if chunk.duration_ms > self._config.max_frame_ms:
            msg = (
                f"VAD chunks must be at most {self._config.max_frame_ms} ms; "
                f"got {chunk.duration_ms:.1f} ms"
            )
            raise ValueError(msg)

        frames: list[VadFrame] = []
        frame_bytes = self._frame_samples * _BYTES_PER_SAMPLE
        data = chunk.data
        offset = 0
        while offset < len(data):
            block = data[offset : offset + frame_bytes]
            offset += frame_bytes
            samples = len(block) // _BYTES_PER_SAMPLE
            duration_ms = self._frame_duration_ms(samples)
            db = self._loudness_db(block)
            voiced = db >= self._threshold_db()
            self._adapt_noise_floor(db)
            self._frames += 1
            if voiced:
                self._voiced_frames += 1
            frame = self._advance(
                db=db,
                voiced=voiced,
                duration_ms=duration_ms,
                frame_index=self._frame_index,
            )
            self._frame_index += 1
            frames.append(frame)
        return tuple(frames)


@dataclass(frozen=True)
class TurnDecision:
    """A deterministic end-of-turn decision."""

    action: Literal["continue", "hold", "commit", "discard"]
    reason: str
    text: str


@dataclass(frozen=True)
class EndOfTurnConfig:
    """Tuning for semantic end-of-turn detection; no model is used."""

    silence_commit_ms: int = 600
    punctuation_silence_ms: int = 200
    max_utterance_ms: int = 30_000
    min_commit_words: int = 1
    trailing_fillers: frozenset[str] = _DEFAULT_TRAILING_FILLERS

    def __post_init__(self) -> None:
        if self.silence_commit_ms < 0:
            msg = "EndOfTurnConfig.silence_commit_ms must be zero or greater"
            raise TurnConfigError(msg)
        if self.punctuation_silence_ms < 0:
            msg = "EndOfTurnConfig.punctuation_silence_ms must be zero or greater"
            raise TurnConfigError(msg)
        if self.max_utterance_ms < 1:
            msg = "EndOfTurnConfig.max_utterance_ms must be at least one"
            raise TurnConfigError(msg)
        if self.min_commit_words < 1:
            msg = "EndOfTurnConfig.min_commit_words must be at least one"
            raise TurnConfigError(msg)
        if not isinstance(self.trailing_fillers, frozenset):
            msg = "EndOfTurnConfig.trailing_fillers must be a frozenset"
            raise TurnConfigError(msg)
        if any(not isinstance(word, str) or not word.strip() for word in self.trailing_fillers):
            msg = "EndOfTurnConfig.trailing_fillers must contain non-empty strings"
            raise TurnConfigError(msg)


class EndOfTurnDetector:
    """Deterministic semantic end-of-turn detector.

    Rules are evaluated in this order:

    1. Empty text is ``discard`` when no speech has been confirmed, otherwise
       ``continue``.
    2. A trailing filler or conjunction is ``hold``; the user is still going.
    3. An STT endpoint with non-empty text is ``commit``.
    4. Terminal punctuation plus enough silence is ``commit``.
    5. Enough trailing silence plus enough words is ``commit``.
    6. A monologue past ``max_utterance_ms`` is forcibly ``commit``.
    7. Everything else is ``continue``.

    Odd input never raises: non-string text is converted with ``str``.
    """

    def __init__(self, config: EndOfTurnConfig | None = None) -> None:
        self._config = config if config is not None else EndOfTurnConfig()
        self._observations = 0
        self._commits = 0
        self._holds = 0
        self._discards = 0
        self._last_action: str | None = None
        self._last_reason: str | None = None

    def reset(self) -> None:
        """Clear counters and the last decision."""
        self._observations = 0
        self._commits = 0
        self._holds = 0
        self._discards = 0
        self._last_action = None
        self._last_reason = None

    def snapshot(self) -> dict[str, object]:
        """Return counters only; never transcript text."""
        return {
            "observations": self._observations,
            "commits": self._commits,
            "holds": self._holds,
            "discards": self._discards,
            "last_action": self._last_action,
            "last_reason": self._last_reason,
        }

    def _record(self, decision: TurnDecision) -> TurnDecision:
        self._observations += 1
        if decision.action == "commit":
            self._commits += 1
        elif decision.action == "hold":
            self._holds += 1
        elif decision.action == "discard":
            self._discards += 1
        self._last_action = decision.action
        self._last_reason = decision.reason
        return decision

    def observe(
        self,
        *,
        text: str,
        is_final: bool,
        endpoint: bool,
        speech_ms: int,
        silence_ms: int,
    ) -> TurnDecision:
        """Return the end-of-turn decision for one transcript update."""
        normalized = _normalize_text(str(text))
        if not normalized:
            if speech_ms == 0:
                return self._record(TurnDecision("discard", "empty_text", normalized))
            return self._record(TurnDecision("continue", "empty_text", normalized))

        last = _last_word(normalized)
        if last in self._config.trailing_fillers:
            return self._record(TurnDecision("hold", "trailing_filler", normalized))

        if endpoint:
            return self._record(TurnDecision("commit", "stt_endpoint", normalized))

        if (
            normalized[-1] in _TERMINAL_PUNCTUATION
            and silence_ms >= self._config.punctuation_silence_ms
        ):
            return self._record(TurnDecision("commit", "punctuation_and_silence", normalized))

        if (
            silence_ms >= self._config.silence_commit_ms
            and _word_count(normalized) >= self._config.min_commit_words
        ):
            return self._record(TurnDecision("commit", "trailing_silence", normalized))

        if speech_ms >= self._config.max_utterance_ms:
            return self._record(TurnDecision("commit", "max_utterance", normalized))

        return self._record(TurnDecision("continue", "awaiting_more", normalized))


class TurnState(enum.Enum):
    """The lifecycle state of one relay turn."""

    LISTENING = "LISTENING"
    COMMITTED = "COMMITTED"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    INTERRUPTED = "INTERRUPTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class TurnRecord:
    """Mutable state for one turn; the ledger owns transitions."""

    turn_id: str
    state: TurnState
    started_at: float
    run_id: str | None = None
    utterance: str | None = None
    interrupted_at: float | None = None
    interrupt_reason: str | None = None
    interrupt_speech_ms: int | None = None
    flush_latency_ms: float | None = None


@dataclass(frozen=True)
class TurnEvent:
    """One bounded ledger event."""

    kind: Literal[
        "turn.started",
        "turn.committed",
        "turn.speaking",
        "turn.interrupted",
        "turn.completed",
        "turn.failed",
        "barge_in.flushed",
        "barge_in.suppressed",
    ]
    turn_id: str
    at: float
    detail: Mapping[str, object] = field(default_factory=dict)


_ALLOWED_TRANSITIONS: dict[TurnState, frozenset[TurnState]] = {
    TurnState.LISTENING: frozenset({TurnState.COMMITTED, TurnState.FAILED}),
    TurnState.COMMITTED: frozenset({TurnState.THINKING, TurnState.FAILED}),
    TurnState.THINKING: frozenset(
        {TurnState.SPEAKING, TurnState.INTERRUPTED, TurnState.COMPLETED, TurnState.FAILED}
    ),
    TurnState.SPEAKING: frozenset({TurnState.INTERRUPTED, TurnState.COMPLETED, TurnState.FAILED}),
    TurnState.INTERRUPTED: frozenset({TurnState.COMPLETED, TurnState.FAILED}),
}

_STATE_EVENT_KINDS: dict[TurnState, str] = {
    TurnState.COMMITTED: "turn.committed",
    TurnState.THINKING: "turn.speaking",
    TurnState.SPEAKING: "turn.speaking",
    TurnState.INTERRUPTED: "turn.interrupted",
    TurnState.COMPLETED: "turn.completed",
    TurnState.FAILED: "turn.failed",
}


class TurnLedger:
    """Bounded turn-event history with validated transitions."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_events: int = _MAX_EVENTS,
    ) -> None:
        if not callable(clock):
            msg = "TurnLedger clock must be callable"
            raise TurnConfigError(msg)
        if max_events < 1:
            msg = "TurnLedger max_events must be at least one"
            raise TurnConfigError(msg)
        self._clock = clock
        self._max_events = max_events
        self._events: deque[TurnEvent] = deque(maxlen=max_events)
        self._started = 0
        self._committed = 0
        self._speaking = 0
        self._interrupted = 0
        self._completed = 0
        self._failed = 0
        self._flushed = 0
        self._suppressed = 0

    def events(self) -> tuple[TurnEvent, ...]:
        """Return the bounded event history, oldest first."""
        return tuple(self._events)

    def snapshot(self) -> dict[str, object]:
        """Return counters only; never transcripts or audio."""
        return {
            "events": len(self._events),
            "max_events": self._max_events,
            "started": self._started,
            "committed": self._committed,
            "speaking": self._speaking,
            "interrupted": self._interrupted,
            "completed": self._completed,
            "failed": self._failed,
            "flushed": self._flushed,
            "suppressed": self._suppressed,
        }

    def _append(self, kind: str, turn_id: str, detail: Mapping[str, object]) -> None:
        event = TurnEvent(kind=kind, turn_id=turn_id, at=self._clock(), detail=detail)  # type: ignore[arg-type]
        self._events.append(event)
        if kind == "turn.started":
            self._started += 1
        elif kind == "turn.committed":
            self._committed += 1
        elif kind == "turn.speaking":
            self._speaking += 1
        elif kind == "turn.interrupted":
            self._interrupted += 1
        elif kind == "turn.completed":
            self._completed += 1
        elif kind == "turn.failed":
            self._failed += 1
        elif kind == "barge_in.flushed":
            self._flushed += 1
        elif kind == "barge_in.suppressed":
            self._suppressed += 1

    def start(self, turn_id: str | None = None) -> TurnRecord:
        """Create a listening turn and record ``turn.started``."""
        identifier = turn_id if turn_id is not None else secrets.token_hex(4)
        if not isinstance(identifier, str) or not identifier.strip():
            msg = "turn_id must be a non-empty string"
            raise TurnStateError(msg)
        record = TurnRecord(
            turn_id=identifier.strip(),
            state=TurnState.LISTENING,
            started_at=self._clock(),
        )
        self._append("turn.started", record.turn_id, {"state": record.state.value})
        return record

    def transition(self, record: TurnRecord, state: TurnState, **fields: object) -> TurnRecord:
        """Move a record to an allowed state and append its event."""
        if not isinstance(record, TurnRecord):
            msg = "transition requires a TurnRecord"
            raise TurnStateError(msg)
        if not isinstance(state, TurnState):
            msg = "transition requires a TurnState"
            raise TurnStateError(msg)
        allowed = _ALLOWED_TRANSITIONS.get(record.state, frozenset())
        if state not in allowed:
            msg = (
                f"illegal turn transition {record.state.value} -> {state.value} "
                f"for turn {record.turn_id}"
            )
            raise TurnStateError(msg)
        for name in fields:
            if name not in TurnRecord.__dataclass_fields__:
                msg = f"unknown turn field {name!r} for turn {record.turn_id}"
                raise TurnStateError(msg)
        previous = record.state
        record.state = state
        for name, value in fields.items():
            if name != "state":
                setattr(record, name, value)
        kind = _STATE_EVENT_KINDS.get(state, "turn.started")
        detail: dict[str, object] = {"from": previous.value, "to": state.value}
        detail.update(fields)
        self._append(kind, record.turn_id, detail)
        return record

    def mark_interrupted(
        self,
        record: TurnRecord,
        *,
        reason: str,
        speech_ms: int,
        flush_latency_ms: float | None,
        run_id: str | None,
    ) -> TurnRecord:
        """Mark a turn interrupted and append ``turn.interrupted``."""
        return self.transition(
            record,
            TurnState.INTERRUPTED,
            interrupted_at=self._clock(),
            interrupt_reason=reason,
            interrupt_speech_ms=speech_ms,
            flush_latency_ms=flush_latency_ms,
            run_id=run_id,
        )

    def mark_completed(self, record: TurnRecord) -> TurnRecord:
        """Mark a turn completed and append ``turn.completed``."""
        return self.transition(record, TurnState.COMPLETED)


class PlaybackSink(Protocol):
    """A playback queue that can drop everything still queued."""

    def flush(self) -> None: ...


class TtsPlayback:
    """Adapter that turns :meth:`StreamingTts.clear` into a playback flush."""

    def __init__(self, tts: Any) -> None:
        if tts is None or not callable(getattr(tts, "clear", None)):
            msg = "TtsPlayback requires an object with a callable clear() method"
            raise TurnConfigError(msg)
        self._tts = tts

    def flush(self) -> None:
        """Drop queued sentences and buffered audio."""
        self._tts.clear()


@dataclass(frozen=True)
class RunStopResult:
    """The outcome of asking Hermes to stop one run."""

    ok: bool
    run_id: str
    status: str | None
    latency_ms: float
    error: str | None = None


class RunStopper(Protocol):
    """A port that stops an active Hermes run."""

    async def stop(self, run_id: str) -> RunStopResult: ...


def _json_strings(value: object, depth: int = 0) -> list[str]:
    if depth > 4:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        found: list[str] = []
        for item in value.values():
            found.extend(_json_strings(item, depth + 1))
        return found
    if isinstance(value, (list, tuple)):
        found = []
        for item in value:
            found.extend(_json_strings(item, depth + 1))
        return found
    return []


def _body_status(body: object) -> str | None:
    if not isinstance(body, Mapping):
        return None
    for key in ("status", "state"):
        value = body.get(key)
        if isinstance(value, str):
            return value
    run = body.get("run")
    if isinstance(run, Mapping):
        value = run.get("status")
        if isinstance(value, str):
            return value
    return None


def _contains_run_not_active(body: object) -> bool:
    return any("run_not_active" in text.lower() for text in _json_strings(body))


class HttpRunStopper:
    """POST ``/v1/runs/{run_id}/stop`` against the loopback Hermes API."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        client: Any | None = None,
        timeout_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            msg = "HttpRunStopper base_url must be a non-empty string"
            raise TurnConfigError(msg)
        parsed = urlsplit(base_url.strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            msg = "HttpRunStopper base_url must be an http(s) URL with a host"
            raise TurnConfigError(msg)
        if api_key is not None and not isinstance(api_key, str):
            msg = "HttpRunStopper api_key must be a string or None"
            raise TurnConfigError(msg)
        if client is not None and not callable(getattr(client, "post", None)):
            msg = "HttpRunStopper client must provide an async post() method"
            raise TurnConfigError(msg)
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            msg = "HttpRunStopper timeout_s must be finite and greater than zero"
            raise TurnConfigError(msg)
        if not callable(clock):
            msg = "HttpRunStopper clock must be callable"
            raise TurnConfigError(msg)
        self._base_url = base_url.strip().rstrip("/")
        self._api_key = api_key
        self._client = client
        self._timeout_s = timeout_s
        self._clock = clock

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def _post(self, url: str, headers: dict[str, str]) -> Any:
        client = self._client
        owned = False
        if client is None:
            try:
                import httpx
            except ImportError as exc:
                msg = (
                    "httpx is required to stop a Hermes run; "
                    "install the relay 'dev' extra (./relay[dev])"
                )
                raise TurnConfigError(msg) from exc
            client = httpx.AsyncClient(timeout=self._timeout_s)
            owned = True
        try:
            response = client.post(url, headers=headers)
            if inspect.isawaitable(response):
                response = await response
            return response
        finally:
            if owned:
                close = getattr(client, "aclose", None)
                if close is not None:
                    result = close()
                    if inspect.isawaitable(result):
                        await result

    async def _response_body(self, response: Any) -> object:
        try:
            body = response.json()
        except Exception:
            return None
        if inspect.isawaitable(body):
            body = await body
        return body

    async def stop(self, run_id: str) -> RunStopResult:
        """Stop one run without ever raising on a transport failure."""
        if not isinstance(run_id, str) or not run_id.strip():
            msg = "run_id must be a non-empty string"
            raise TurnConfigError(msg)
        run_id = run_id.strip()
        started = self._clock()
        status: str | None = None
        error: str | None = None
        ok = False
        try:
            url = f"{self._base_url}/v1/runs/{quote(run_id, safe='')}/stop"
            response = await self._post(url, self._headers())
            code = int(getattr(response, "status_code", 0))
            body = await self._response_body(response)
            status = _body_status(body)
            if code == 200:
                ok = True
            elif code == 409 and _contains_run_not_active(body):
                error = "run_not_active"
            else:
                error = f"http_{code}"
        except TurnConfigError:
            raise
        except Exception as exc:
            error = type(exc).__name__
        latency_ms = max((self._clock() - started) * _MILLISECONDS_PER_SECOND, 0.0)
        return RunStopResult(
            ok=ok,
            run_id=run_id,
            status=status,
            latency_ms=latency_ms,
            error=error,
        )


@dataclass(frozen=True)
class BargeInResult:
    """The barge-in decision for one VAD frame."""

    flushed: bool
    flush_latency_ms: float | None
    within_budget: bool
    stop_attempted: bool
    stop_accepted: bool
    run_stop: RunStopResult | None
    suppressed_reason: str | None


@dataclass(frozen=True)
class MicOutcome:
    """One processed microphone frame."""

    frame: VadFrame
    barge_in: BargeInResult | None = None


@dataclass(frozen=True)
class Interruption:
    """A confirmed interruption and its one-shot marker."""

    turn_id: str
    run_id: str | None
    reason: str
    speech_ms: int
    flush_latency_ms: float | None
    marker: str
    run_stop: RunStopResult | None


class TurnController:
    """Coordinates VAD, end-of-turn decisions, playback, and run stopping.

    A single event loop owns the controller; it is not thread-safe.  Audio is
    inspected locally and only the injected :class:`RunStopper` may touch the
    network.
    """

    def __init__(
        self,
        *,
        vad: VadEngine,
        detector: EndOfTurnDetector,
        gate: Any,
        playback: PlaybackSink | None = None,
        stopper: RunStopper | None = None,
        clock: Callable[[], float] = time.monotonic,
        flush_budget_ms: float = DEFAULT_FLUSH_BUDGET_MS,
    ) -> None:
        if vad is None or not callable(getattr(vad, "process", None)):
            msg = "TurnController vad must provide process()"
            raise TurnConfigError(msg)
        if detector is None or not callable(getattr(detector, "observe", None)):
            msg = "TurnController detector must provide observe()"
            raise TurnConfigError(msg)
        if gate is None or not callable(getattr(gate, "consider", None)):
            msg = "TurnController gate must provide consider()"
            raise TurnConfigError(msg)
        if playback is not None and not callable(getattr(playback, "flush", None)):
            msg = "TurnController playback must provide flush()"
            raise TurnConfigError(msg)
        if stopper is not None and not callable(getattr(stopper, "stop", None)):
            msg = "TurnController stopper must provide stop()"
            raise TurnConfigError(msg)
        if not callable(clock):
            msg = "TurnController clock must be callable"
            raise TurnConfigError(msg)
        if not math.isfinite(flush_budget_ms) or flush_budget_ms <= 0:
            msg = "TurnController flush_budget_ms must be finite and greater than zero"
            raise TurnConfigError(msg)

        self._vad = vad
        self._detector = detector
        self._gate = gate
        self._playback = playback
        self._stopper = stopper
        self._clock = clock
        self._flush_budget_ms = flush_budget_ms
        self._ledger = TurnLedger(clock=clock)
        self._active: TurnRecord | None = None
        self._pending_marker: str | None = None
        self._spoken = ""
        self._spoken_truncated = False
        self._utterance_flushed = False
        self._utterance_considered = False
        self._utterance_suppressed = False
        self._last_flush_latency_ms: float | None = None
        self._last_speech_ms = 0
        self._last_silence_ms = 0
        self._barge_in_flushed = 0
        self._interruptions = 0
        self._false_triggers = 0
        self._stops_attempted = 0
        self._stops_accepted = 0
        self._over_budget = 0
        self._session_alive = True
        self._vad_model: str | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        playback: PlaybackSink | None = None,
        stopper: RunStopper | None = None,
    ) -> TurnController:
        """Build a controller from settings without making a network call."""
        min_speech_ms = int(getattr(settings, "barge_in_min_speech_ms", 300))
        cooldown_ms = int(getattr(settings, "barge_in_cooldown_ms", 1000))
        base_url = getattr(settings, "hermes_base_url", "http://127.0.0.1:8642")
        api_key_value = getattr(settings, "hermes_api_key", None)
        api_key: str | None
        if api_key_value is not None and hasattr(api_key_value, "get_secret_value"):
            api_key = str(api_key_value.get_secret_value())
        elif api_key_value is None:
            api_key = None
        else:
            api_key = str(api_key_value)

        resolved_stopper = stopper
        if resolved_stopper is None:
            resolved_stopper = HttpRunStopper(str(base_url), api_key)

        controller = cls(
            vad=EnergyVad(VadConfig()),
            detector=EndOfTurnDetector(),
            gate=BargeInGate(
                min_speech_ms=min_speech_ms,
                cooldown_ms=cooldown_ms,
                clock=time.monotonic,
            ),
            playback=playback,
            stopper=resolved_stopper,
            clock=time.monotonic,
            flush_budget_ms=DEFAULT_FLUSH_BUDGET_MS,
        )
        controller._vad_model = getattr(settings, "vad_model", None)
        return controller

    @property
    def ledger(self) -> TurnLedger:
        """The turn ledger, for diagnostics and tests."""
        return self._ledger

    def _require_active(self) -> TurnRecord:
        if self._active is None:
            msg = "no active turn; call begin_user_turn first"
            raise TurnStateError(msg)
        return self._active

    def _require_state(self, record: TurnRecord, allowed: frozenset[TurnState]) -> None:
        if record.state not in allowed:
            expected = ", ".join(state.value for state in sorted(allowed, key=lambda s: s.value))
            msg = f"turn {record.turn_id} is {record.state.value}; expected one of {expected}"
            raise TurnStateError(msg)

    def begin_user_turn(self, turn_id: str | None = None) -> TurnRecord:
        """Start a new listening turn; a pending interruption marker survives."""
        record = self._ledger.start(turn_id)
        self._active = record
        self._spoken = ""
        self._spoken_truncated = False
        self._reset_utterance()
        return record

    def commit_utterance(self, text: str) -> TurnRecord:
        """Commit the current utterance: LISTENING -> COMMITTED."""
        record = self._require_active()
        self._require_state(record, frozenset({TurnState.LISTENING}))
        normalized = _normalize_text(str(text))
        return self._ledger.transition(record, TurnState.COMMITTED, utterance=normalized)

    def begin_reply(self, *, run_id: str | None = None) -> TurnRecord:
        """Begin a reply: COMMITTED -> THINKING, and -> SPEAKING with a run id."""
        record = self._require_active()
        self._require_state(record, frozenset({TurnState.COMMITTED}))
        record = self._ledger.transition(record, TurnState.THINKING, run_id=run_id)
        if run_id is not None:
            record = self._ledger.transition(record, TurnState.SPEAKING)
        self._active = record
        return record

    def note_spoken(self, text: str) -> None:
        """Record spoken text for the bounded interruption marker tail."""
        normalized = _sanitize_spoken(str(text))
        if not normalized:
            return
        combined = f"{self._spoken} {normalized}".strip() if self._spoken else normalized
        if len(combined) > MAX_SPOKEN_TAIL_CHARS:
            combined = combined[-MAX_SPOKEN_TAIL_CHARS:]
            if " " in combined:
                combined = combined[combined.index(" ") + 1 :]
            self._spoken_truncated = True
        self._spoken = combined
        if self._active is not None and self._active.state is TurnState.THINKING:
            self._active = self._ledger.transition(self._active, TurnState.SPEAKING)

    def end_reply(self) -> TurnRecord:
        """Complete a reply: THINKING/SPEAKING/INTERRUPTED -> COMPLETED."""
        record = self._require_active()
        self._require_state(
            record,
            frozenset({TurnState.THINKING, TurnState.SPEAKING, TurnState.INTERRUPTED}),
        )
        record = self._ledger.mark_completed(record)
        self._active = record
        self._reset_utterance()
        return record

    def _reset_utterance(self) -> None:
        self._utterance_flushed = False
        self._utterance_considered = False
        self._utterance_suppressed = False
        self._last_flush_latency_ms = None

    def _gate_min_speech_ms(self) -> int:
        value = getattr(self._gate, "_min_speech_ms", 1)
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 1

    def _flush_playback(self, frame: VadFrame) -> tuple[bool, float | None]:
        if frame.speech_started_at is None:
            latency_ms: float | None = None
        else:
            latency_ms = max(
                (self._clock() - frame.speech_started_at) * _MILLISECONDS_PER_SECOND,
                0.0,
            )
        if self._playback is None:
            return False, latency_ms
        try:
            self._playback.flush()
        except Exception:
            _LOGGER.exception(
                "barge_in.flush_failed",
                extra={"event": "barge_in.flush_failed"},
            )
            return False, latency_ms
        return True, latency_ms

    def _on_onset(self, record: TurnRecord, frame: VadFrame) -> BargeInResult:
        self._reset_utterance()
        flushed, latency_ms = self._flush_playback(frame)
        self._utterance_flushed = True
        self._last_flush_latency_ms = latency_ms
        within_budget = latency_ms is not None and latency_ms <= self._flush_budget_ms
        if latency_ms is not None and latency_ms > self._flush_budget_ms:
            self._over_budget += 1
            _LOGGER.warning(
                "barge_in.flush_over_budget",
                extra={
                    "event": "barge_in.flush_over_budget",
                    "flush_latency_ms": latency_ms,
                    "flush_budget_ms": self._flush_budget_ms,
                },
            )
        if flushed:
            self._barge_in_flushed += 1
        self._ledger._append(
            "barge_in.flushed",
            record.turn_id,
            {
                "flushed": flushed,
                "flush_latency_ms": latency_ms,
                "within_budget": within_budget,
            },
        )
        return BargeInResult(
            flushed=flushed,
            flush_latency_ms=latency_ms,
            within_budget=within_budget,
            stop_attempted=False,
            stop_accepted=False,
            run_stop=None,
            suppressed_reason=None,
        )

    async def _confirm(self, record: TurnRecord, frame: VadFrame) -> BargeInResult:
        self._stops_attempted += 1
        run_stop: RunStopResult | None = None
        if record.run_id is not None and self._stopper is not None:
            try:
                result = await self._stopper.stop(record.run_id)
                if isinstance(result, RunStopResult):
                    run_stop = result
                else:
                    run_stop = RunStopResult(
                        ok=False,
                        run_id=record.run_id,
                        status=None,
                        latency_ms=0.0,
                        error="invalid_stopper_result",
                    )
            except Exception as exc:
                run_stop = RunStopResult(
                    ok=False,
                    run_id=record.run_id,
                    status=None,
                    latency_ms=0.0,
                    error=type(exc).__name__,
                )
        record = self._ledger.mark_interrupted(
            record,
            reason="barge_in",
            speech_ms=frame.speech_ms,
            flush_latency_ms=self._last_flush_latency_ms,
            run_id=record.run_id,
        )
        self._active = record
        self._interruptions += 1
        self._stops_accepted += 1
        self._pending_marker = self._build_marker(frame.speech_ms)
        return BargeInResult(
            flushed=self._utterance_flushed,
            flush_latency_ms=self._last_flush_latency_ms,
            within_budget=(
                self._last_flush_latency_ms is not None
                and self._last_flush_latency_ms <= self._flush_budget_ms
            ),
            stop_attempted=True,
            stop_accepted=True,
            run_stop=run_stop,
            suppressed_reason=None,
        )

    def _suppress(self, record: TurnRecord, frame: VadFrame, reason: str) -> BargeInResult:
        self._false_triggers += 1
        self._utterance_suppressed = True
        self._ledger._append(
            "barge_in.suppressed",
            record.turn_id,
            {"reason": reason, "speech_ms": frame.speech_ms},
        )
        return BargeInResult(
            flushed=self._utterance_flushed,
            flush_latency_ms=self._last_flush_latency_ms,
            within_budget=(
                self._last_flush_latency_ms is not None
                and self._last_flush_latency_ms <= self._flush_budget_ms
            ),
            stop_attempted=False,
            stop_accepted=False,
            run_stop=None,
            suppressed_reason=reason,
        )

    async def _process_frame(self, frame: VadFrame) -> MicOutcome:
        record = self._active
        if record is None or record.state is not TurnState.SPEAKING or not self._session_alive:
            return MicOutcome(frame=frame)

        onset_result = self._on_onset(record, frame) if frame.onset else None

        min_speech_ms = self._gate_min_speech_ms()
        if frame.speech_ms >= min_speech_ms and not self._utterance_considered:
            self._utterance_considered = True
            accepted = self._gate.consider(frame.speech_ms)
            if accepted:
                return MicOutcome(frame=frame, barge_in=await self._confirm(record, frame))
            return MicOutcome(
                frame=frame,
                barge_in=self._suppress(record, frame, "cooldown"),
            )

        if frame.offset:
            barge_in: BargeInResult | None = onset_result
            if (
                frame.speech_ms > 0
                and frame.speech_ms < min_speech_ms
                and not self._utterance_considered
                and not self._utterance_suppressed
            ):
                barge_in = self._suppress(record, frame, "too_short")
            self._reset_utterance()
            return MicOutcome(frame=frame, barge_in=barge_in)

        return MicOutcome(frame=frame, barge_in=onset_result)

    async def feed_mic(self, chunk: PcmChunk) -> MicOutcome:
        """Process one microphone chunk and return its last frame outcome.

        A multi-frame chunk is consumed in order.  The returned outcome
        describes the final frame; per-frame barge-in events are recorded in
        the ledger.
        """
        frames = self._vad.process(chunk)
        outcome = MicOutcome(frame=frames[-1])
        for frame in frames:
            self._last_speech_ms = frame.speech_ms
            self._last_silence_ms = frame.silence_ms
            outcome = await self._process_frame(frame)
        return outcome

    def on_transcript(self, event: Any) -> TurnDecision:
        """Classify one transcript update with the semantic detector."""
        text = ""
        is_final = False
        endpoint = False
        if isinstance(event, TranscriptEvent):
            text = event.text
            is_final = event.kind == "final"
            endpoint = event.is_endpoint
        else:
            text = str(getattr(event, "text", ""))
            is_final = getattr(event, "kind", "") == "final"
            endpoint = bool(getattr(event, "is_endpoint", False))
        return self._detector.observe(
            text=text,
            is_final=is_final,
            endpoint=endpoint,
            speech_ms=self._last_speech_ms,
            silence_ms=self._last_silence_ms,
        )

    def _build_marker(self, speech_ms: int) -> str:
        tail = ""
        if self._spoken:
            bounded = self._spoken
            if len(bounded) > MAX_SPOKEN_TAIL_CHARS:
                bounded = bounded[-MAX_SPOKEN_TAIL_CHARS:]
                if " " in bounded:
                    bounded = bounded[bounded.index(" ") + 1 :]
                self._spoken_truncated = True
            if self._spoken_truncated:
                bounded = f"…{bounded}"
            tail = f' while saying "{bounded}"'
        return INTERRUPTION_MARKER_TEMPLATE.format(speech_ms=speech_ms, tail=tail)

    async def stop_reply(self, *, reason: str = "barge_in", speech_ms: int = 0) -> Interruption:
        """Explicitly interrupt a THINKING or SPEAKING reply."""
        record = self._require_active()
        self._require_state(
            record,
            frozenset({TurnState.THINKING, TurnState.SPEAKING}),
        )
        flush_latency_ms: float | None = None
        if record.state is TurnState.SPEAKING and self._playback is not None:
            started = self._clock()
            try:
                self._playback.flush()
            except Exception:
                _LOGGER.exception(
                    "barge_in.stop_reply_flush_failed",
                    extra={"event": "barge_in.stop_reply_flush_failed"},
                )
            flush_latency_ms = max(
                (self._clock() - started) * _MILLISECONDS_PER_SECOND,
                0.0,
            )
        run_stop: RunStopResult | None = None
        if record.run_id is not None:
            self._stops_attempted += 1
            if self._stopper is not None:
                try:
                    result = await self._stopper.stop(record.run_id)
                    if isinstance(result, RunStopResult):
                        run_stop = result
                    else:
                        run_stop = RunStopResult(
                            ok=False,
                            run_id=record.run_id,
                            status=None,
                            latency_ms=0.0,
                            error="invalid_stopper_result",
                        )
                except Exception as exc:
                    run_stop = RunStopResult(
                        ok=False,
                        run_id=record.run_id,
                        status=None,
                        latency_ms=0.0,
                        error=type(exc).__name__,
                    )
            if run_stop is None or run_stop.ok:
                self._stops_accepted += 1
        record = self._ledger.mark_interrupted(
            record,
            reason=reason,
            speech_ms=speech_ms,
            flush_latency_ms=flush_latency_ms,
            run_id=record.run_id,
        )
        self._active = record
        self._interruptions += 1
        marker = self._build_marker(speech_ms)
        self._pending_marker = marker
        return Interruption(
            turn_id=record.turn_id,
            run_id=record.run_id,
            reason=reason,
            speech_ms=speech_ms,
            flush_latency_ms=flush_latency_ms,
            marker=marker,
            run_stop=run_stop,
        )

    def take_marker(self) -> str | None:
        """Return and clear the pending interruption marker exactly once."""
        marker = self._pending_marker
        self._pending_marker = None
        return marker

    def with_interruption_marker(
        self, parts: Sequence[Mapping[str, object]]
    ) -> list[dict[str, object]]:
        """Return a new parts list, prepending the one-shot marker if pending."""
        copied = [dict(part) for part in parts]
        marker = self.take_marker()
        if marker is None:
            return copied
        return [{"type": "text", "text": marker}, *copied]

    def snapshot(self) -> dict[str, object]:
        """Return counters, the active turn id/state, and component snapshots."""
        active = self._active
        return {
            "flushed": self._barge_in_flushed,
            "interruptions": self._interruptions,
            "false_triggers": self._false_triggers,
            "stops_attempted": self._stops_attempted,
            "stops_accepted": self._stops_accepted,
            "over_budget": self._over_budget,
            "marker_pending": self._pending_marker is not None,
            "turn_id": active.turn_id if active is not None else None,
            "turn_state": active.state.value if active is not None else None,
            "vad": self._vad.snapshot(),
            "detector": self._detector.snapshot(),
            "session_alive": self._session_alive,
        }

    def close(self) -> None:
        """Mark the session closed and clear the pending marker; never stops a run."""
        self._session_alive = False
        self._pending_marker = None


__all__ = [
    "DEFAULT_FLUSH_BUDGET_MS",
    "INTERRUPTION_MARKER_TEMPLATE",
    "MAX_SPOKEN_TAIL_CHARS",
    "BargeInResult",
    "EndOfTurnConfig",
    "EndOfTurnDetector",
    "EnergyVad",
    "HttpRunStopper",
    "Interruption",
    "MicOutcome",
    "PlaybackSink",
    "RunStopResult",
    "RunStopper",
    "TtsPlayback",
    "TurnConfigError",
    "TurnController",
    "TurnDecision",
    "TurnError",
    "TurnEvent",
    "TurnLedger",
    "TurnRecord",
    "TurnState",
    "TurnStateError",
    "VadConfig",
    "VadEngine",
    "VadFrame",
    "VadState",
]
