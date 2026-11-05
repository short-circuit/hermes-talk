"""Sentence-level text-to-speech and Opus streaming.

The public entry point is :class:`StreamingTts`: text deltas are split into
speakable sentences, synthesized locally with Kokoro, encoded as 48 kHz Opus,
and yielded to the caller.  The queue is bounded and every overflow fails with
an explicit :class:`SpeechBackpressureExceeded` instead of growing without
limit.  Nothing here contacts the network.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from relay.speech.audio import OpusEncoder, PcmChunk, pcm16_from_float32
from relay.speech.backpressure import BufferLimits
from relay.speech.errors import (
    SpeechBackpressureExceeded,
    SpeechError,
    SpeechModelUnavailable,
    SpeechStateError,
)

_BOUNDARY_CHARS = frozenset({".", "!", "?", "…"})
_CLAUSE_MARKERS = frozenset({",", ";", ":"})
_ABBREVIATIONS = frozenset(
    {
        "mr.",
        "mrs.",
        "dr.",
        "ms.",
        "st.",
        "vs.",
        "e.g.",
        "i.e.",
        "etc.",
        "u.s.",
    }
)
_TOKEN_RE = re.compile(r"(\S+)$")
_MIN_SEGMENT_CHARS = 2
_MIN_FIRST_SEGMENT_CHARS = 4
_MAX_FIRST_SEGMENT_CHARS = 400
_KOKORO_SAMPLE_RATE = 24000
_TTS_QUEUE_NAME = "tts_sentence_queue"


def _monotonic() -> float:
    """Monotonic clock, isolated so tests can substitute a fake clock."""
    import time

    return time.monotonic()


class SentenceSplitter:
    """Incremental sentence boundary detector for streaming model output.

    Boundaries are ``.``/``!``/``?``/``…`` followed by whitespace or the end of
    the buffer, plus newlines.  Decimals, common abbreviations, and dots inside
    URL-ish tokens are not treated as boundaries.  Long comma/semicolon/colon
    clauses are emitted early so the first spoken sentence stays short.
    """

    def __init__(
        self,
        *,
        clause_split_chars: int = 80,
        max_segment_chars: int = 160,
        first_segment_max_chars: int = 20,
    ) -> None:
        if clause_split_chars < _MIN_SEGMENT_CHARS:
            msg = "clause_split_chars must be at least two"
            raise ValueError(msg)
        if max_segment_chars < clause_split_chars:
            msg = "max_segment_chars must be at least clause_split_chars"
            raise ValueError(msg)
        if not _MIN_FIRST_SEGMENT_CHARS <= first_segment_max_chars <= _MAX_FIRST_SEGMENT_CHARS:
            msg = (
                "first_segment_max_chars must be between "
                f"{_MIN_FIRST_SEGMENT_CHARS} and {_MAX_FIRST_SEGMENT_CHARS}"
            )
            raise ValueError(msg)
        self._clause_split_chars = clause_split_chars
        self._max_segment_chars = max_segment_chars
        self._first_segment_max_chars = first_segment_max_chars
        self._first_segment_emitted = False
        self._buffer = ""

    @property
    def pending(self) -> str:
        """The un-emitted remainder, for diagnostics only."""
        return self._buffer

    def _is_protected_dot(self, prefix: str) -> bool:
        stripped = prefix.strip()
        if not stripped:
            return False
        match = _TOKEN_RE.search(stripped)
        if match is None:
            return False
        return match.group(1).lower() in _ABBREVIATIONS

    def _split_capped(self, text: str) -> list[str]:
        segments: list[str] = []
        remaining = text.strip()
        while len(remaining) > self._max_segment_chars:
            cut = remaining.rfind(" ", 0, self._max_segment_chars + 1)
            if cut < _MIN_SEGMENT_CHARS:
                cut = self._max_segment_chars
            head = remaining[:cut].strip()
            if len(head) >= _MIN_SEGMENT_CHARS:
                segments.append(head)
            remaining = remaining[cut:].strip()
        if len(remaining) >= _MIN_SEGMENT_CHARS:
            segments.append(remaining)
        return segments

    def _cap_first_segment(self, segment: str) -> tuple[str, str]:
        """Split a first segment at the last word boundary inside the cap."""
        cap = self._first_segment_max_chars
        if len(segment) <= cap:
            return segment, ""
        cut = segment.rfind(" ", 0, cap + 1)
        if cut < _MIN_SEGMENT_CHARS:
            cut = cap
        prefix = segment[:cut].strip()
        remainder = segment[cut:].strip()
        return prefix, remainder

    def _emit(self, segments: list[str]) -> tuple[list[str], bool]:
        """Emit segments, applying the first-segment cap once per stream.

        When the first segment is capped, its remainder and any later segments
        are put back at the front of the buffer so the caller can emit the
        short prefix immediately and keep the rest for the next drain.
        """
        if not segments or self._first_segment_emitted:
            return segments, False
        self._first_segment_emitted = True
        prefix, remainder = self._cap_first_segment(segments[0])
        if not remainder:
            return segments, False
        rest = [remainder, *segments[1:]]
        tail = " ".join(rest)
        self._buffer = f"{tail} {self._buffer}".strip() if self._buffer else tail
        return ([prefix] if prefix else []), True

    def _find_boundary(self) -> int | None:
        for index, char in enumerate(self._buffer):
            if char == "\n":
                return index
            if char not in _BOUNDARY_CHARS:
                continue
            at_end = index + 1 == len(self._buffer)
            followed_by_space = index + 1 < len(self._buffer) and self._buffer[index + 1].isspace()
            if not (at_end or followed_by_space):
                continue
            if char == "." and self._is_protected_dot(self._buffer[: index + 1]):
                continue
            return index
        return None

    def _consume(self, end: int) -> str:
        segment = self._buffer[:end].strip()
        self._buffer = self._buffer[end:].lstrip()
        return segment

    def _drain(self) -> list[str]:
        emitted: list[str] = []
        while self._buffer:
            boundary = self._find_boundary()
            marker_index: int | None = None
            if len(self._buffer) > self._clause_split_chars:
                marker_index = next(
                    (index for index, char in enumerate(self._buffer) if char in _CLAUSE_MARKERS),
                    None,
                )
            if marker_index is not None and (boundary is None or marker_index < boundary):
                segment = self._consume(marker_index + 1)
                if len(segment) >= _MIN_SEGMENT_CHARS:
                    new_segments, capped = self._emit(self._split_capped(segment))
                    emitted.extend(new_segments)
                    if capped:
                        return emitted
                continue

            if boundary is not None:
                if self._buffer[boundary] == "\n":
                    segment = self._consume(boundary)
                    if segment:
                        new_segments, capped = self._emit(self._split_capped(segment))
                        emitted.extend(new_segments)
                        if capped:
                            return emitted
                    self._buffer = self._buffer.lstrip("\n").lstrip()
                    continue
                segment = self._consume(boundary + 1)
                if segment:
                    new_segments, capped = self._emit(self._split_capped(segment))
                    emitted.extend(new_segments)
                    if capped:
                        return emitted
                continue

            if len(self._buffer) > self._max_segment_chars:
                cut = self._buffer.rfind(" ", 0, self._max_segment_chars + 1)
                if cut < _MIN_SEGMENT_CHARS:
                    cut = self._max_segment_chars
                segment = self._consume(cut)
                if len(segment) >= _MIN_SEGMENT_CHARS:
                    new_segments, capped = self._emit(self._split_capped(segment))
                    emitted.extend(new_segments)
                    if capped:
                        return emitted
                continue

            break
        return emitted

    def push(self, delta: str) -> list[str]:
        """Append a text delta and return every completed sentence."""
        if not delta:
            return []
        self._buffer += delta
        return self._drain()

    def flush(self) -> list[str]:
        """Return the remaining buffered text as final segment(s)."""
        remainder = self._buffer.strip()
        self._buffer = ""
        if not remainder:
            return []
        segments = self._split_capped(remainder)
        if self._first_segment_emitted or not segments:
            return segments
        self._first_segment_emitted = True
        prefix, rest = self._cap_first_segment(segments[0])
        emitted = [prefix] if prefix else []
        if rest:
            emitted.append(rest)
        emitted.extend(segments[1:])
        return emitted

    def reset(self) -> None:
        """Drop any buffered text and re-arm the first-segment cap."""
        self._buffer = ""
        self._first_segment_emitted = False


@dataclass(frozen=True)
class AudioChunk:
    """Opus audio for one synthesized sentence."""

    opus_packets: tuple[bytes, ...]
    duration_ms: float
    sentence_index: int
    text: str
    first_audio_ms: float | None


@dataclass
class TtsMetrics:
    """Counters for one streaming TTS session."""

    sentences: int = 0
    packets: int = 0
    audio_ms: float = 0.0
    first_audio_ms: list[float] = field(default_factory=list)
    dropped_sentences: int = 0
    backpressure_events: int = 0


class KokoroTtsEngine:
    """Local Kokoro ONNX synthesis engine."""

    name = "kokoro"

    def __init__(self, model_path: Path | None, voices_path: Path | None) -> None:
        if model_path is None:
            msg = (
                "kokoro TTS model is not configured: set TTS_MODEL_PATH to a local "
                "Kokoro ONNX model file (for example kokoro-v1.0.int8.onnx)"
            )
            raise SpeechModelUnavailable(msg)
        if voices_path is None:
            msg = (
                "kokoro TTS voices file is not configured: set TTS_VOICES_PATH to "
                "the matching voices .bin file"
            )
            raise SpeechModelUnavailable(msg)

        model = Path(model_path).expanduser()
        voices = Path(voices_path).expanduser()
        if not model.is_file():
            msg = (
                f"kokoro TTS model file does not exist: {model}; set TTS_MODEL_PATH "
                "to a local Kokoro ONNX model file"
            )
            raise SpeechModelUnavailable(msg)
        if not voices.is_file():
            msg = (
                f"kokoro TTS voices file does not exist: {voices}; set TTS_VOICES_PATH "
                "to the matching voices .bin file"
            )
            raise SpeechModelUnavailable(msg)

        self._model_path = model
        self._voices_path = voices
        self._kokoro: Any = None
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        try:
            import kokoro_onnx
        except ImportError as exc:
            raise SpeechModelUnavailable(
                "kokoro-onnx is not installed: install the relay 'speech' extra (./relay[speech])"
            ) from exc
        self._kokoro = kokoro_onnx.Kokoro(str(self._model_path), str(self._voices_path))

    async def synthesize(
        self,
        text: str,
        *,
        voice: str = "af_sarah",
        speed: float = 1.0,
    ) -> PcmChunk:
        """Synthesize one sentence to 24 kHz PCM16."""
        normalized = text.strip()
        if not normalized:
            msg = "TTS text must not be empty"
            raise ValueError(msg)
        async with self._lock:
            samples, sample_rate = await asyncio.to_thread(
                self._kokoro.create,
                normalized,
                voice=voice,
                speed=speed,
                lang="en-us",
            )
        return pcm16_from_float32(samples, sample_rate)

    def describe(self) -> dict[str, object]:
        return {
            "engine": self.name,
            "model": self._model_path.name,
            "voices": self._voices_path.name,
            "sample_rate": _KOKORO_SAMPLE_RATE,
            "default_voice": "af_sarah",
        }


class StreamingTts:
    """Bounded sentence queue, synthesis worker, and Opus packet stream."""

    def __init__(
        self,
        engine: Any,
        *,
        voice: str = "af_sarah",
        speed: float = 1.0,
        opus_bit_rate: int = 32000,
        max_pending_sentences: int = 3,
        backpressure_timeout_s: float = 2.0,
        first_segment_max_chars: int = 20,
        limits: BufferLimits | None = None,
    ) -> None:
        if max_pending_sentences < 1:
            msg = "max_pending_sentences must be at least one"
            raise ValueError(msg)
        if backpressure_timeout_s < 0:
            msg = "backpressure_timeout_s must be zero or greater"
            raise ValueError(msg)
        if opus_bit_rate <= 0:
            msg = "opus_bit_rate must be greater than zero"
            raise ValueError(msg)
        if not _MIN_FIRST_SEGMENT_CHARS <= first_segment_max_chars <= _MAX_FIRST_SEGMENT_CHARS:
            msg = (
                "first_segment_max_chars must be between "
                f"{_MIN_FIRST_SEGMENT_CHARS} and {_MAX_FIRST_SEGMENT_CHARS}"
            )
            raise ValueError(msg)

        self._engine = engine
        self._voice = voice
        self._speed = speed
        self._opus_bit_rate = opus_bit_rate
        self._max_pending_sentences = max_pending_sentences
        self._backpressure_timeout_s = backpressure_timeout_s
        self._first_segment_max_chars = first_segment_max_chars
        self._limits = limits or BufferLimits(
            max_bytes=480000,
            max_frames=250,
            name="tts_audio",
        )
        self._splitter = SentenceSplitter(first_segment_max_chars=first_segment_max_chars)
        self._sentences: asyncio.Queue[tuple[str, float, int, int]] = asyncio.Queue(
            maxsize=max_pending_sentences
        )
        self._sentence_ready = asyncio.Event()
        self._output: deque[AudioChunk] = deque()
        self._output_ready = asyncio.Event()
        self._pending_bytes = 0
        self._pending_frames = 0
        self._metrics = TtsMetrics()
        self._worker: asyncio.Task[None] | None = None
        self._encoder: OpusEncoder | None = None
        self._closed = False
        self._end_of_stream = False
        self._done = False
        self._error: BaseException | None = None
        self._generation = 0
        self._next_sentence_index = 0

    def _ensure_open(self) -> None:
        if self._closed:
            msg = "TTS stream is closed"
            raise SpeechStateError(msg)

    async def _enqueue_sentence(self, sentence: str) -> None:
        item = (
            sentence,
            _monotonic(),
            self._next_sentence_index,
            self._generation,
        )
        self._next_sentence_index += 1
        if self._sentences.full():
            self._metrics.backpressure_events += 1
            timeout = self._backpressure_timeout_s
            try:
                if timeout is None:
                    await self._sentences.put(item)
                else:
                    await asyncio.wait_for(self._sentences.put(item), timeout=timeout)
            except TimeoutError:
                msg = (
                    f"TTS sentence queue is full: limit={self._max_pending_sentences} "
                    f"pending sentences, observed={self._sentences.qsize() + 1}"
                )
                raise SpeechBackpressureExceeded(
                    _TTS_QUEUE_NAME,
                    self._max_pending_sentences,
                    self._sentences.qsize() + 1,
                    detail=msg,
                ) from None
        else:
            self._sentences.put_nowait(item)
        self._sentence_ready.set()

    async def push_text(self, delta: str) -> None:
        """Split a text delta and enqueue each completed sentence."""
        self._ensure_open()
        for sentence in self._splitter.push(delta):
            await self._enqueue_sentence(sentence)

    async def flush(self) -> None:
        """Enqueue the splitter remainder and mark the end of the stream."""
        self._ensure_open()
        for sentence in self._splitter.flush():
            await self._enqueue_sentence(sentence)
        self._end_of_stream = True
        self._sentence_ready.set()

    async def warmup(self, *, text: str = "Ready.") -> float:
        """Synthesize and encode one sentence to warm caches, returning elapsed ms.

        The call runs the same engine synthesis and Opus encoding path as the
        background worker, but discards the encoded packets instead of queueing
        an :class:`AudioChunk`.  It therefore does not touch
        :attr:`TtsMetrics.first_audio_ms`, :attr:`TtsMetrics.sentences`, or the
        consumer-visible queue.

        This exists so callers and benchmarks can separate the one-time cost of
        loading the engine and running the first inference from the steady-state
        first-audio latency.  Call it before :meth:`push_text` (or on a throwaway
        instance) and report the returned duration as the cold-start component.
        The method is idempotent: repeated calls leave no accumulated state and
        the next sentence can still be encoded normally.  It raises
        :class:`SpeechStateError` once :meth:`close` has been called.
        """
        self._ensure_open()
        started = time.perf_counter()
        result = self._engine.synthesize(
            text,
            voice=self._voice,
            speed=self._speed,
        )
        if inspect.isawaitable(result):
            result = await result
        self._ensure_open()
        pcm = result
        await asyncio.to_thread(self._encode_sentence, pcm)
        self._ensure_open()
        return (time.perf_counter() - started) * 1000.0

    def _ensure_worker(self) -> None:
        if self._worker is None and not self._closed:
            self._worker = asyncio.create_task(self._worker_loop())

    async def _next_sentence(self) -> tuple[str, float, int, int] | None:
        while True:
            if self._closed:
                return None
            try:
                return self._sentences.get_nowait()
            except asyncio.QueueEmpty:
                if self._end_of_stream:
                    return None
                self._sentence_ready.clear()
                if not self._sentences.empty():
                    continue
                if self._end_of_stream:
                    return None
                await self._sentence_ready.wait()

    def _encode_sentence(self, pcm: PcmChunk) -> tuple[list[bytes], float]:
        encoder = OpusEncoder(bit_rate=self._opus_bit_rate)
        self._encoder = encoder
        try:
            packets = encoder.encode(pcm)
            packets.extend(encoder.flush())
        finally:
            encoder.close()
        return packets, pcm.duration_ms

    def _enqueue_audio(self, chunk: AudioChunk) -> None:
        size = sum(len(packet) for packet in chunk.opus_packets)
        frames = len(chunk.opus_packets)
        self._output.append(chunk)
        self._pending_bytes += size
        self._pending_frames += frames
        dropped = 0
        while self._output and (
            self._pending_bytes > self._limits.max_bytes
            or self._pending_frames > self._limits.max_frames
        ):
            old = self._output.popleft()
            self._pending_bytes -= sum(len(packet) for packet in old.opus_packets)
            self._pending_frames -= len(old.opus_packets)
            dropped += 1
        if dropped:
            self._metrics.dropped_sentences += dropped
            self._metrics.backpressure_events += 1

        self._metrics.sentences += 1
        self._metrics.packets += frames
        self._metrics.audio_ms += chunk.duration_ms
        if chunk.first_audio_ms is not None:
            self._metrics.first_audio_ms.append(chunk.first_audio_ms)
        self._output_ready.set()

    async def _worker_loop(self) -> None:
        try:
            while True:
                item = await self._next_sentence()
                if item is None:
                    break
                sentence, enqueued_at, index, generation = item
                result = self._engine.synthesize(
                    sentence,
                    voice=self._voice,
                    speed=self._speed,
                )
                if inspect.isawaitable(result):
                    result = await result
                if self._closed:
                    break
                pcm = result
                packets, duration_ms = await asyncio.to_thread(
                    self._encode_sentence,
                    pcm,
                )
                if self._closed or generation != self._generation:
                    self._metrics.dropped_sentences += 1
                    continue
                first_audio_ms = (_monotonic() - enqueued_at) * 1000.0
                self._enqueue_audio(
                    AudioChunk(
                        opus_packets=tuple(packets),
                        duration_ms=duration_ms,
                        sentence_index=index,
                        text=sentence,
                        first_audio_ms=first_audio_ms,
                    )
                )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._error = exc
        finally:
            self._done = True
            self._output_ready.set()

    async def chunks(self) -> AsyncIterator[AudioChunk]:
        """Yield one Opus chunk per synthesized sentence."""
        self._ensure_worker()
        while True:
            if self._error is not None:
                raise self._error
            if self._output:
                chunk = self._output.popleft()
                self._pending_bytes -= sum(len(packet) for packet in chunk.opus_packets)
                self._pending_frames -= len(chunk.opus_packets)
                yield chunk
                continue
            if self._done:
                break
            self._output_ready.clear()
            if self._output or self._done:
                continue
            await self._output_ready.wait()

    async def close(self) -> None:
        """Cancel synthesis, close the encoder, and clear all buffers."""
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        worker = self._worker
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            self._worker = None
        if self._encoder is not None:
            self._encoder.close()
            self._encoder = None
        self._splitter.reset()
        while not self._sentences.empty():
            self._sentences.get_nowait()
        self._output.clear()
        self._pending_bytes = 0
        self._pending_frames = 0
        self._done = True
        self._output_ready.set()

    def clear(self) -> None:
        """Drop queued sentences and buffered audio (barge-in support)."""
        dropped = 0
        while not self._sentences.empty():
            self._sentences.get_nowait()
            dropped += 1
        dropped += len(self._output)
        self._output.clear()
        self._pending_bytes = 0
        self._pending_frames = 0
        self._generation += 1
        self._splitter.reset()
        self._metrics.dropped_sentences += dropped

    def metrics(self) -> TtsMetrics:
        """Return the live metrics object for this stream."""
        return self._metrics

    def describe(self) -> dict[str, object]:
        """Return the configured streaming parameters for diagnostics."""
        return {
            "voice": self._voice,
            "speed": self._speed,
            "opus_bit_rate": self._opus_bit_rate,
            "max_pending_sentences": self._max_pending_sentences,
            "first_segment_max_chars": self._first_segment_max_chars,
        }


def _validate_tts_engine(settings: Any) -> None:
    engine_name = str(settings.tts_engine).strip().lower()
    if engine_name != "kokoro":
        msg = f"unsupported TTS engine {settings.tts_engine!r}: expected 'kokoro'"
        raise SpeechError(msg)


def _tts_limits(settings: Any) -> BufferLimits:
    return BufferLimits(
        max_bytes=settings.speech_max_buffer_bytes,
        max_frames=settings.speech_max_buffer_frames,
        name="tts_audio",
    )


def _build_streaming_tts(settings: Any, engine: Any) -> StreamingTts:
    return StreamingTts(
        engine,
        voice=settings.tts_voice,
        speed=settings.tts_speed,
        opus_bit_rate=settings.opus_bit_rate,
        max_pending_sentences=settings.tts_max_pending_sentences,
        backpressure_timeout_s=settings.speech_backpressure_timeout_s,
        first_segment_max_chars=settings.tts_first_segment_max_chars,
        limits=_tts_limits(settings),
    )


class _LazyTtsEngine:
    """Construct the Kokoro engine on first synthesis, not at app boot.

    The engine loads an ONNX model and a voices file.  Building it eagerly in
    ``create_app`` would add hundreds of milliseconds to every boot even when
    no call is placed, so the factory defers the load until the first reply.
    The wrapper is shared by every :class:`StreamingTts` built from one factory
    and construction is serialized for the rare concurrent first call.
    """

    name = "kokoro"

    def __init__(self, model_path: Any, voices_path: Any) -> None:
        self._model_path = model_path
        self._voices_path = voices_path
        self._engine: KokoroTtsEngine | None = None
        self._lock = threading.Lock()

    def _get(self) -> KokoroTtsEngine:
        with self._lock:
            if self._engine is None:
                self._engine = KokoroTtsEngine(self._model_path, self._voices_path)
            return self._engine

    async def synthesize(
        self,
        text: str,
        *,
        voice: str = "af_sarah",
        speed: float = 1.0,
    ) -> PcmChunk:
        engine = await asyncio.to_thread(self._get)
        return await engine.synthesize(text, voice=voice, speed=speed)

    def describe(self) -> dict[str, object]:
        engine = self._engine
        if engine is not None:
            return engine.describe()
        model = Path(self._model_path).name if self._model_path is not None else None
        voices = Path(self._voices_path).name if self._voices_path is not None else None
        return {
            "engine": self.name,
            "model": model,
            "voices": voices,
            "sample_rate": _KOKORO_SAMPLE_RATE,
            "default_voice": "af_sarah",
            "loaded": False,
        }


def create_tts(settings: Any) -> StreamingTts:
    """Build the configured streaming TTS pipeline."""
    _validate_tts_engine(settings)
    engine = KokoroTtsEngine(settings.tts_model_path, settings.tts_voices_path)
    return _build_streaming_tts(settings, engine)


def create_tts_factory(settings: Any) -> Callable[[], StreamingTts]:
    """Return a factory that builds a fresh TTS stream per reply.

    The returned callable shares one lazily constructed Kokoro engine across
    every stream, so the model is loaded once per process.  Each call returns a
    new :class:`StreamingTts` because a stream cannot be reused after
    :meth:`StreamingTts.flush`.
    """
    _validate_tts_engine(settings)
    engine = _LazyTtsEngine(settings.tts_model_path, settings.tts_voices_path)

    def _factory() -> StreamingTts:
        return _build_streaming_tts(settings, engine)

    return _factory
