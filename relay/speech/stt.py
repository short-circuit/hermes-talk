"""Local streaming speech-to-text adapters.

Two engines are provided:

* :class:`SherpaStreamingStt` uses a sherpa-onnx streaming transducer and emits
  rate-limited partial hypotheses plus endpoint/flush finals.
* :class:`WhisperFallbackStt` is a pseudo-streaming fallback built on
  faster-whisper.  It intentionally emits **no partial hypotheses**: audio is
  buffered and transcribed only when the buffer reaches ``chunk_ms`` or when
  :meth:`flush` is called.

Both engines run blocking decoder work in a worker thread and serialize access
with an :class:`asyncio.Lock`.  Nothing here contacts the network.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from relay.speech.audio import PcmChunk, _import_numpy, resample_pcm16
from relay.speech.errors import (
    SpeechError,
    SpeechModelUnavailable,
    SpeechStateError,
)

_STT_SAMPLE_RATE = 16000
_MAX_FRAME_MS = 500.0
_EXPECTED_SHERPA_FILES = "tokens.txt, encoder-*.onnx, decoder-*.onnx and joiner-*.onnx"


def _monotonic() -> float:
    """Monotonic clock, isolated so tests can substitute a fake clock."""
    return time.monotonic()


def _normalize_text(text: str) -> str:
    return " ".join(text.split()).strip()


def _result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    return str(getattr(result, "text", ""))


def _select_model(root: Path, prefix: str) -> Path | None:
    candidates = sorted(root.glob(f"{prefix}-*.onnx"))
    if not candidates:
        return None
    for candidate in candidates:
        if candidate.name.endswith(".int8.onnx"):
            return candidate
    return candidates[0]


@dataclass(frozen=True)
class TranscriptEvent:
    """One partial or final transcript update."""

    kind: Literal["partial", "final"]
    text: str
    engine: str
    latency_ms: float
    audio_ms: float
    is_endpoint: bool = False


class SttSession(Protocol):
    """A single utterance's streaming decoder session."""

    async def feed(self, chunk: PcmChunk) -> tuple[TranscriptEvent, ...]: ...

    async def flush(self) -> TranscriptEvent | None: ...

    async def close(self) -> None: ...


class SttAdapter(Protocol):
    """A configured speech-to-text engine that can start sessions."""

    name: str

    def describe(self) -> dict[str, object]: ...

    async def start_session(self) -> SttSession: ...


def _resolve_sherpa_model(model_dir: Path | None) -> tuple[Path, Path, Path, Path]:
    if model_dir is None:
        msg = (
            "sherpa-onnx STT model directory is not configured: set STT_MODEL_DIR "
            f"to a directory containing {_EXPECTED_SHERPA_FILES}"
        )
        raise SpeechModelUnavailable(msg)
    root = Path(model_dir).expanduser()
    if not root.is_dir():
        msg = (
            f"sherpa-onnx STT model directory does not exist: {root}; set STT_MODEL_DIR "
            f"to a directory containing {_EXPECTED_SHERPA_FILES}"
        )
        raise SpeechModelUnavailable(msg)

    tokens = root / "tokens.txt"
    if not tokens.is_file():
        msg = (
            f"sherpa-onnx STT model directory is missing tokens.txt: {root}; "
            f"set STT_MODEL_DIR to a directory containing {_EXPECTED_SHERPA_FILES}"
        )
        raise SpeechModelUnavailable(msg)

    encoder = _select_model(root, "encoder")
    decoder = _select_model(root, "decoder")
    joiner = _select_model(root, "joiner")
    missing = [
        name
        for name, path in (
            ("encoder-*.onnx", encoder),
            ("decoder-*.onnx", decoder),
            ("joiner-*.onnx", joiner),
        )
        if path is None
    ]
    if missing:
        msg = (
            f"sherpa-onnx STT model directory is missing {', '.join(missing)}: {root}; "
            f"set STT_MODEL_DIR to a directory containing {_EXPECTED_SHERPA_FILES}"
        )
        raise SpeechModelUnavailable(msg)
    return tokens, encoder, decoder, joiner  # type: ignore[return-value]


class _SherpaSession:
    """One streaming transducer session for a single utterance."""

    def __init__(self, adapter: SherpaStreamingStt, recognizer: Any) -> None:
        self._adapter = adapter
        self._recognizer = recognizer
        self._stream = recognizer.create_stream()
        self._lock = asyncio.Lock()
        self._closed = False
        self._audio_ms = 0.0
        self._last_partial_text = ""
        self._last_partial_at = -math.inf

    def _ensure_open(self) -> None:
        if self._closed:
            msg = "STT session is closed"
            raise SpeechStateError(msg)

    def _decode(self, data: bytes, sample_rate: int) -> tuple[str, bool]:
        numpy = _import_numpy()
        samples = numpy.frombuffer(data, dtype=numpy.int16).astype(numpy.float32)
        samples /= 32768.0
        self._stream.accept_waveform(sample_rate, samples)
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)
        text = _result_text(self._recognizer.get_result(self._stream))
        endpoint = bool(self._recognizer.is_endpoint(self._stream))
        return text, endpoint

    def _decode_pending(self) -> tuple[str, bool]:
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)
        text = _result_text(self._recognizer.get_result(self._stream))
        endpoint = bool(self._recognizer.is_endpoint(self._stream))
        return text, endpoint

    def _reset_utterance(self) -> None:
        self._recognizer.reset(self._stream)
        self._audio_ms = 0.0
        self._last_partial_text = ""
        self._last_partial_at = -math.inf

    async def feed(self, chunk: PcmChunk) -> tuple[TranscriptEvent, ...]:
        self._ensure_open()
        chunk.validate()
        if chunk.duration_ms > _MAX_FRAME_MS:
            msg = "STT frames must be at most 500 ms; callers send 20 ms frames"
            raise ValueError(msg)

        data = chunk.data
        if chunk.sample_rate != _STT_SAMPLE_RATE:
            if not self._adapter.resample_input:
                msg = f"sherpa-onnx STT requires 16 kHz mono PCM16, got {chunk.sample_rate} Hz"
                raise ValueError(msg)
            data = await asyncio.to_thread(
                resample_pcm16,
                chunk.data,
                chunk.sample_rate,
                _STT_SAMPLE_RATE,
            )

        started = _monotonic()
        async with self._lock:
            text, endpoint = await asyncio.to_thread(
                self._decode,
                data,
                _STT_SAMPLE_RATE,
            )
            decode_ms = (_monotonic() - started) * 1000.0
            self._audio_ms += chunk.duration_ms
            normalized = _normalize_text(text)
            events: list[TranscriptEvent] = []

            if endpoint:
                events.append(
                    TranscriptEvent(
                        kind="final",
                        text=normalized,
                        engine=self._adapter.name,
                        latency_ms=decode_ms,
                        audio_ms=self._audio_ms,
                        is_endpoint=True,
                    )
                )
                self._reset_utterance()
                return tuple(events)

            now = _monotonic()
            elapsed_ms = (now - self._last_partial_at) * 1000.0
            if (
                normalized
                and normalized != self._last_partial_text
                and elapsed_ms >= self._adapter.partial_interval_ms
            ):
                events.append(
                    TranscriptEvent(
                        kind="partial",
                        text=normalized,
                        engine=self._adapter.name,
                        latency_ms=decode_ms,
                        audio_ms=self._audio_ms,
                    )
                )
                self._last_partial_text = normalized
                self._last_partial_at = now
            return tuple(events)

    async def flush(self) -> TranscriptEvent | None:
        self._ensure_open()
        started = _monotonic()
        async with self._lock:
            text, endpoint = await asyncio.to_thread(self._decode_pending)
            latency_ms = (_monotonic() - started) * 1000.0
            normalized = _normalize_text(text)
            audio_ms = self._audio_ms
            self._reset_utterance()
        if not normalized:
            return None
        return TranscriptEvent(
            kind="final",
            text=normalized,
            engine=self._adapter.name,
            latency_ms=latency_ms,
            audio_ms=audio_ms,
            is_endpoint=endpoint,
        )

    async def close(self) -> None:
        self._closed = True


class SherpaStreamingStt:
    """Streaming sherpa-onnx transducer adapter (16 kHz mono PCM16 input)."""

    name = "sherpa"

    def __init__(
        self,
        model_dir: Path | None,
        *,
        num_threads: int = 4,
        provider: str = "cpu",
        partial_interval_ms: int = 120,
        rule2_min_trailing_silence: float = 0.8,
        resample_input: bool = True,
    ) -> None:
        if num_threads < 1:
            msg = "num_threads must be at least one"
            raise ValueError(msg)
        if partial_interval_ms <= 0:
            msg = "partial_interval_ms must be greater than zero"
            raise ValueError(msg)
        if rule2_min_trailing_silence < 0:
            msg = "rule2_min_trailing_silence must be zero or greater"
            raise ValueError(msg)

        tokens, encoder, decoder, joiner = _resolve_sherpa_model(model_dir)
        self.model_dir = Path(model_dir).expanduser()  # type: ignore[arg-type]
        self.num_threads = num_threads
        self.provider = provider
        self.partial_interval_ms = partial_interval_ms
        self.rule2_min_trailing_silence = rule2_min_trailing_silence
        self.resample_input = resample_input
        self._tokens = tokens
        self._encoder = encoder
        self._decoder = decoder
        self._joiner = joiner
        self._recognizer = self._build_recognizer()

    def _build_recognizer(self) -> Any:
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise SpeechModelUnavailable(
                "sherpa-onnx is not installed: install the relay 'speech' extra (./relay[speech])"
            ) from exc
        return sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(self._tokens),
            encoder=str(self._encoder),
            decoder=str(self._decoder),
            joiner=str(self._joiner),
            num_threads=self.num_threads,
            sample_rate=_STT_SAMPLE_RATE,
            feature_dim=80,
            decoding_method="greedy_search",
            enable_endpoint_detection=True,
            rule2_min_trailing_silence=self.rule2_min_trailing_silence,
            provider=self.provider,
        )

    def describe(self) -> dict[str, object]:
        return {
            "engine": "sherpa-onnx",
            "model_dir": str(self.model_dir),
            "sample_rate": _STT_SAMPLE_RATE,
            "num_threads": self.num_threads,
            "provider": self.provider,
            "partial_interval_ms": self.partial_interval_ms,
            "streaming": True,
        }

    async def start_session(self) -> SttSession:
        return _SherpaSession(self, self._recognizer)


def _load_whisper_model(model_dir: Path, *, cpu_threads: int) -> Any:
    try:
        import faster_whisper
    except ImportError as exc:
        raise SpeechModelUnavailable(
            "faster-whisper is not installed: install the relay 'speech' extra (./relay[speech])"
        ) from exc
    return faster_whisper.WhisperModel(
        str(model_dir),
        device="cpu",
        compute_type="int8",
        cpu_threads=cpu_threads,
        local_files_only=True,
    )


class _WhisperSession:
    """Pseudo-streaming faster-whisper session with no partial hypotheses."""

    def __init__(self, adapter: WhisperFallbackStt, model: Any) -> None:
        self._adapter = adapter
        self._model = model
        self._lock = asyncio.Lock()
        self._closed = False
        self._buffer = bytearray()
        self._audio_ms = 0.0

    def _ensure_open(self) -> None:
        if self._closed:
            msg = "STT session is closed"
            raise SpeechStateError(msg)

    def _transcribe(self, data: bytes) -> list[str]:
        numpy = _import_numpy()
        samples = numpy.frombuffer(bytes(data), dtype=numpy.int16).astype(numpy.float32)
        samples /= 32768.0
        segments, _info = self._model.transcribe(
            samples,
            beam_size=1,
            language=self._adapter.language,
        )
        texts: list[str] = []
        for segment in segments:
            text = str(getattr(segment, "text", "")).strip()
            if text:
                texts.append(text)
        return texts

    def _take_buffer(self) -> tuple[bytes, float]:
        data = bytes(self._buffer)
        audio_ms = self._audio_ms
        self._buffer.clear()
        self._audio_ms = 0.0
        return data, audio_ms

    async def feed(self, chunk: PcmChunk) -> tuple[TranscriptEvent, ...]:
        self._ensure_open()
        chunk.validate()
        data = chunk.data
        if chunk.sample_rate != _STT_SAMPLE_RATE:
            data = await asyncio.to_thread(
                resample_pcm16,
                chunk.data,
                chunk.sample_rate,
                _STT_SAMPLE_RATE,
            )
        self._buffer.extend(data)
        self._audio_ms += chunk.duration_ms
        if self._audio_ms < self._adapter.chunk_ms:
            return ()

        started = _monotonic()
        async with self._lock:
            data, audio_ms = self._take_buffer()
            texts = await asyncio.to_thread(self._transcribe, data)
            latency_ms = (_monotonic() - started) * 1000.0
        return tuple(
            TranscriptEvent(
                kind="final",
                text=text,
                engine=self._adapter.name,
                latency_ms=latency_ms,
                audio_ms=audio_ms,
            )
            for text in texts
        )

    async def flush(self) -> TranscriptEvent | None:
        self._ensure_open()
        if not self._buffer:
            return None
        started = _monotonic()
        async with self._lock:
            data, audio_ms = self._take_buffer()
            texts = await asyncio.to_thread(self._transcribe, data)
            latency_ms = (_monotonic() - started) * 1000.0
        if not texts:
            return None
        return TranscriptEvent(
            kind="final",
            text=" ".join(texts),
            engine=self._adapter.name,
            latency_ms=latency_ms,
            audio_ms=audio_ms,
        )

    async def close(self) -> None:
        self._closed = True


class WhisperFallbackStt:
    """Pseudo-streaming faster-whisper fallback.

    This adapter emits no partial hypotheses.  It buffers audio and emits final
    events only when the buffer reaches ``chunk_ms`` or on :meth:`flush`.
    """

    name = "whisper"

    def __init__(
        self,
        model_dir: Path | None,
        *,
        chunk_ms: int = 2000,
        cpu_threads: int = 8,
        language: str = "en",
    ) -> None:
        if chunk_ms <= 0:
            msg = "chunk_ms must be greater than zero"
            raise ValueError(msg)
        if cpu_threads < 1:
            msg = "cpu_threads must be at least one"
            raise ValueError(msg)
        if model_dir is None:
            msg = (
                "faster-whisper model directory is not configured: set "
                "WHISPER_MODEL_DIR to a local CTranslate2 model directory"
            )
            raise SpeechModelUnavailable(msg)
        root = Path(model_dir).expanduser()
        if not root.is_dir():
            msg = (
                f"faster-whisper model directory does not exist: {root}; set "
                "WHISPER_MODEL_DIR to a local CTranslate2 model directory"
            )
            raise SpeechModelUnavailable(msg)
        self.model_dir = root
        self.chunk_ms = chunk_ms
        self.cpu_threads = cpu_threads
        self.language = language
        self._model = _load_whisper_model(root, cpu_threads=cpu_threads)

    def describe(self) -> dict[str, object]:
        return {
            "engine": "faster-whisper",
            "model_dir": str(self.model_dir),
            "sample_rate": _STT_SAMPLE_RATE,
            "cpu_threads": self.cpu_threads,
            "chunk_ms": self.chunk_ms,
            "streaming": False,
        }

    async def start_session(self) -> SttSession:
        return _WhisperSession(self, self._model)


def create_stt(settings: Any) -> SttAdapter:
    """Build the configured STT adapter."""
    engine = str(settings.stt_engine).strip().lower()
    if engine == "sherpa":
        return SherpaStreamingStt(
            settings.stt_model_dir,
            num_threads=settings.stt_num_threads,
            partial_interval_ms=settings.stt_partial_interval_ms,
        )
    if engine == "whisper":
        return WhisperFallbackStt(
            settings.whisper_model_dir,
            chunk_ms=settings.whisper_chunk_ms,
        )
    msg = f"unsupported STT engine {settings.stt_engine!r}: expected 'sherpa' or 'whisper'"
    raise SpeechError(msg)
