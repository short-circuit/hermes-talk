"""Integration tests against real local speech models.

These tests are skipped unless ``SPEECH_MODEL_DIR`` and both ``TTS_MODEL_PATH``
and ``TTS_VOICES_PATH`` point at local model files, and the speech packages
import.  They never download anything.
"""

from __future__ import annotations

import asyncio
import os
import wave
from pathlib import Path

import pytest

pytest.importorskip("sherpa_onnx")
pytest.importorskip("kokoro_onnx")
pytest.importorskip("av")

from relay.speech.audio import PcmChunk
from relay.speech.stt import SherpaStreamingStt
from relay.speech.tts import KokoroTtsEngine, StreamingTts

_SPEECH_MODEL_DIR = os.environ.get("SPEECH_MODEL_DIR")
_TTS_MODEL_PATH = os.environ.get("TTS_MODEL_PATH")
_TTS_VOICES_PATH = os.environ.get("TTS_VOICES_PATH")
_HAVE_MODELS = bool(
    _SPEECH_MODEL_DIR
    and _TTS_MODEL_PATH
    and _TTS_VOICES_PATH
    and Path(_SPEECH_MODEL_DIR).is_dir()
    and Path(_TTS_MODEL_PATH).is_file()
    and Path(_TTS_VOICES_PATH).is_file()
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _HAVE_MODELS,
        reason="SPEECH_MODEL_DIR, TTS_MODEL_PATH and TTS_VOICES_PATH are required",
    ),
]

_FRAME_MS = 20


def _read_wav(path: Path) -> tuple[int, bytes]:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            pytest.skip("integration wav must be mono PCM16")
        sample_rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    return sample_rate, frames


def test_real_sherpa_streaming_transcript_latency() -> None:
    model_dir = Path(_SPEECH_MODEL_DIR)  # type: ignore[arg-type]
    wav_path = model_dir / "test_wavs" / "0.wav"
    if not wav_path.is_file():
        pytest.skip("test_wavs/0.wav is not present in the speech model directory")
    sample_rate, pcm = _read_wav(wav_path)

    async def scenario() -> list[object]:
        adapter = SherpaStreamingStt(model_dir)
        session = await adapter.start_session()
        events: list[object] = []
        frame_bytes = int(sample_rate * _FRAME_MS / 1000) * 2
        try:
            for offset in range(0, len(pcm), frame_bytes):
                chunk = PcmChunk(
                    data=pcm[offset : offset + frame_bytes],
                    sample_rate=sample_rate,
                )
                if chunk.data:
                    events.extend(await session.feed(chunk))
            final = await session.flush()
            if final is not None:
                events.append(final)
        finally:
            await session.close()
        return events

    events = asyncio.run(scenario())
    finals = [event for event in events if event.kind == "final"]
    assert finals, "expected at least one final transcript"
    transcript = " ".join(event.text for event in finals)
    assert any(character.isalpha() for character in transcript)
    assert len(transcript.split()) >= 3
    assert all(event.latency_ms < 300 for event in finals)


def test_real_kokoro_first_audio_latency() -> None:
    engine = KokoroTtsEngine(
        Path(_TTS_MODEL_PATH),  # type: ignore[arg-type]
        Path(_TTS_VOICES_PATH),  # type: ignore[arg-type]
    )

    async def scenario() -> list[object]:
        tts = StreamingTts(engine, backpressure_timeout_s=2.0)
        try:
            warmup_ms = await tts.warmup()
            assert warmup_ms > 0
            assert tts.metrics().first_audio_ms == []
            assert tts.metrics().sentences == 0
            await tts.push_text("Yes.")
            await tts.flush()
            return [chunk async for chunk in tts.chunks()]
        finally:
            await tts.close()

    chunks = asyncio.run(scenario())
    assert chunks, "expected at least one synthesized audio chunk"
    first = chunks[0]
    assert first.opus_packets
    assert all(packet for packet in first.opus_packets)
    assert first.first_audio_ms is not None
    assert first.first_audio_ms < 250
