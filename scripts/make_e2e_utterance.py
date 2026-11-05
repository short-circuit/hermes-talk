"""Generate the scripted-utterance fixture used by the container E2E call test.

Synthesizes one sentence with the local Kokoro voice, resamples it to the
16 kHz mono PCM16 that the relay's microphone bridge expects, and writes a plain
RIFF/WAVE file.  Nothing is downloaded: the model files are the ones the relay
container mounts.  The output is synthetic speech, so it is safe to commit.

Usage:
    HERMES_TALK_MODELS=/path/to/models .venv/bin/python scripts/make_e2e_utterance.py <output.wav> ["<text>"]

`HERMES_TALK_MODELS` defaults to the archive host's model directory, the same
one `deploy/.env` points the relay container at.
"""

from __future__ import annotations

import asyncio
import os
import sys
import wave
from pathlib import Path

import av

from relay.speech.audio import PcmChunk
from relay.speech.tts import KokoroTtsEngine

MODELS = Path(os.environ.get("HERMES_TALK_MODELS", "/store/archive/hermes-talk-models"))
DEFAULT_TEXT = "What time is it?"
TARGET_RATE = 16000
# The relay's streaming recognizer ends a turn on trailing silence, so the
# fixture carries a realistic silence tail (and a short lead-in) exactly like a
# person pausing after a question.
LEAD_SILENCE_MS = 200
TAIL_SILENCE_MS = 1600


def _resample(chunk: PcmChunk) -> bytes:
    """Resample one PCM16 chunk to 16 kHz mono."""
    resampler = av.AudioResampler(format="s16", layout="mono", rate=TARGET_RATE)
    frame = av.AudioFrame(format="s16", layout="mono", samples=chunk.frames)
    frame.sample_rate = chunk.sample_rate
    frame.planes[0].update(chunk.data)
    parts: list[bytes] = []
    for resampled in resampler.resample(frame):
        parts.append(bytes(resampled.planes[0])[: resampled.samples * 2])
    for resampled in resampler.resample(None):
        parts.append(bytes(resampled.planes[0])[: resampled.samples * 2])
    return b"".join(parts)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip())
        return 2
    output = Path(argv[1])
    text = argv[2] if len(argv) > 2 else DEFAULT_TEXT

    engine = KokoroTtsEngine(
        MODELS / "kokoro-v1.0.int8.onnx", MODELS / "voices-v1.0.bin"
    )
    chunk = asyncio.run(engine.synthesize(text, voice="af_sarah", speed=0.95))
    pcm = _resample(chunk)
    lead = b"\x00\x00" * (TARGET_RATE * LEAD_SILENCE_MS // 1000)
    tail = b"\x00\x00" * (TARGET_RATE * TAIL_SILENCE_MS // 1000)
    pcm = lead + pcm + tail

    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(TARGET_RATE)
        handle.writeframes(pcm)

    seconds = len(pcm) / 2 / TARGET_RATE
    print(f"wrote {output} ({len(pcm)} bytes, {seconds:.2f}s, text={text!r})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
