"""PCM16 helpers, resampling, and Opus encoding.

All audio crossing a module boundary here is 16-bit little-endian mono PCM.
``numpy`` and ``av`` are imported lazily so that importing this module never
fails on a host that installed only the relay's development extras.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from relay.speech.errors import SpeechModelUnavailable, SpeechStateError

_MIN_SAMPLE_RATE = 8000
_MAX_SAMPLE_RATE = 192000
_OPUS_SAMPLE_RATE = 48000
_MAX_OPUS_FRAME_MS = 60.0
_MIN_OPUS_FRAME_MS = 2.5
_BYTES_PER_SAMPLE = 2
_FLOAT32_TO_INT16_SCALE = 32767.0


def _import_numpy() -> Any:
    """Import numpy, translating a missing package into an actionable error."""
    try:
        import numpy
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise SpeechModelUnavailable(
            "numpy is required for speech audio processing; "
            "install the relay 'speech' extra (./relay[speech])"
        ) from exc
    return numpy


def _import_av() -> Any:
    """Import PyAV, translating a missing package into an actionable error."""
    try:
        import av
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise SpeechModelUnavailable(
            "PyAV is required for audio resampling; install the relay 'speech' extra"
        ) from exc
    return av


def _validate_sample_rate(sample_rate: int) -> None:
    if not _MIN_SAMPLE_RATE <= sample_rate <= _MAX_SAMPLE_RATE:
        msg = f"sample rate must be between {_MIN_SAMPLE_RATE} and {_MAX_SAMPLE_RATE} Hz"
        raise ValueError(msg)


@dataclass(frozen=True)
class PcmChunk:
    """A block of mono 16-bit little-endian PCM audio."""

    data: bytes
    sample_rate: int

    @property
    def frames(self) -> int:
        """Number of audio frames (samples) in this chunk."""
        return len(self.data) // _BYTES_PER_SAMPLE

    @property
    def duration_ms(self) -> float:
        """Duration of this chunk in milliseconds."""
        if self.sample_rate <= 0:
            return 0.0
        return self.frames / self.sample_rate * 1000.0

    def validate(self) -> None:
        """Reject empty, misaligned, or implausibly sampled chunks."""
        if not self.data:
            msg = "PCM chunk must not be empty"
            raise ValueError(msg)
        if len(self.data) % _BYTES_PER_SAMPLE:
            msg = "PCM16 data must contain an even number of bytes"
            raise ValueError(msg)
        _validate_sample_rate(self.sample_rate)


def pcm16_from_float32(samples: Any, sample_rate: int) -> PcmChunk:
    """Convert float32 samples in ``[-1, 1]`` to a clipped PCM16 chunk."""
    numpy = _import_numpy()
    _validate_sample_rate(sample_rate)
    array = numpy.asarray(samples, dtype=numpy.float32).ravel()
    clipped = numpy.clip(array, -1.0, 1.0)
    pcm = (clipped * _FLOAT32_TO_INT16_SCALE).astype("<i2")
    chunk = PcmChunk(data=pcm.tobytes(), sample_rate=sample_rate)
    chunk.validate()
    return chunk


def resample_pcm16(data: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample mono PCM16 audio from ``src_rate`` to ``dst_rate``.

    Returns all resampler output concatenated.  An identity conversion returns
    the input bytes unchanged.
    """
    chunk = PcmChunk(data=data, sample_rate=src_rate)
    chunk.validate()
    _validate_sample_rate(dst_rate)
    if src_rate == dst_rate:
        return data

    av = _import_av()
    frame = av.AudioFrame(format="s16", layout="mono", samples=chunk.frames)
    frame.sample_rate = src_rate
    frame.planes[0].update(data)
    resampler = av.AudioResampler(format="s16", layout="mono", rate=dst_rate)
    parts: list[bytes] = []
    for resampled in resampler.resample(frame):
        parts.append(bytes(resampled.planes[0])[: resampled.samples * _BYTES_PER_SAMPLE])
    for resampled in resampler.resample(None):
        parts.append(bytes(resampled.planes[0])[: resampled.samples * _BYTES_PER_SAMPLE])
    return b"".join(parts)


class OpusEncoder:
    """A persistent Opus encoder for mono 48 kHz voice audio.

    ``encode`` accepts PCM16 at any supported sample rate, resamples it to
    48 kHz, and returns one Opus packet per ``frame_ms`` frame in order.
    Remaining samples shorter than a frame are held until the next call or
    emitted by :meth:`flush`.  Call :meth:`flush` before :meth:`close`; the
    encoder cannot be reused after flushing.
    """

    def __init__(
        self,
        *,
        bit_rate: int = 32000,
        frame_ms: float = 20.0,
        application: str = "voip",
    ) -> None:
        if not _MIN_OPUS_FRAME_MS <= frame_ms <= _MAX_OPUS_FRAME_MS:
            msg = f"frame_ms must be between {_MIN_OPUS_FRAME_MS} and {_MAX_OPUS_FRAME_MS}"
            raise ValueError(msg)
        if bit_rate <= 0:
            msg = "bit_rate must be greater than zero"
            raise ValueError(msg)
        if not application:
            msg = "application must not be empty"
            raise ValueError(msg)

        self.bit_rate = bit_rate
        self.frame_ms = frame_ms
        self.application = application
        self._av = _import_av()
        self._frame_samples = int(_OPUS_SAMPLE_RATE * frame_ms / 1000.0)
        if self._frame_samples <= 0:
            msg = "frame_ms produces an empty Opus frame"
            raise ValueError(msg)
        self._pending = bytearray()
        self._closed = False
        self._flushed = False
        self._context = self._open_context()

    def _open_context(self) -> Any:
        av = self._av
        context = av.CodecContext.create(av.Codec("libopus", "w"), "w")
        context.sample_rate = _OPUS_SAMPLE_RATE
        context.layout = "mono"
        context.format = "s16"
        context.bit_rate = self.bit_rate
        context.options = {"application": self.application}
        context.open()
        return context

    def _encode_frame(self, frame_data: bytes) -> list[bytes]:
        frame = self._av.AudioFrame(
            format="s16",
            layout="mono",
            samples=len(frame_data) // _BYTES_PER_SAMPLE,
        )
        frame.sample_rate = _OPUS_SAMPLE_RATE
        frame.planes[0].update(frame_data)
        return [bytes(packet) for packet in self._context.encode(frame)]

    def _drain_full_frames(self) -> list[bytes]:
        frame_bytes = self._frame_samples * _BYTES_PER_SAMPLE
        packets: list[bytes] = []
        while len(self._pending) >= frame_bytes:
            frame_data = bytes(self._pending[:frame_bytes])
            del self._pending[:frame_bytes]
            packets.extend(self._encode_frame(frame_data))
        return packets

    def _ensure_usable(self) -> None:
        if self._closed:
            msg = "Opus encoder is closed"
            raise SpeechStateError(msg)
        if self._flushed:
            msg = "Opus encoder was flushed and cannot be reused"
            raise SpeechStateError(msg)

    def encode(self, chunk: PcmChunk) -> list[bytes]:
        """Encode PCM into Opus packets, resampling to 48 kHz first."""
        self._ensure_usable()
        chunk.validate()
        resampled = resample_pcm16(chunk.data, chunk.sample_rate, _OPUS_SAMPLE_RATE)
        self._pending.extend(resampled)
        return self._drain_full_frames()

    def flush(self) -> list[bytes]:
        """Pad and encode the final partial frame, then drain the codec."""
        self._ensure_usable()
        frame_bytes = self._frame_samples * _BYTES_PER_SAMPLE
        packets: list[bytes] = []
        if self._pending:
            remainder = bytes(self._pending)
            self._pending.clear()
            padding = frame_bytes - len(remainder)
            if padding:
                remainder += bytes(padding)
            packets.extend(self._encode_frame(remainder))
        packets.extend(bytes(packet) for packet in self._context.encode(None))
        self._flushed = True
        return packets

    def close(self) -> None:
        """Release the codec context.  Idempotent; safe in a context manager."""
        if self._closed:
            return
        self._closed = True
        self._pending.clear()
        self._context = None

    def __enter__(self) -> OpusEncoder:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
