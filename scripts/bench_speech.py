#!/usr/bin/env python3
"""Benchmark the local speech adapters.

Measures streaming STT end-of-speech final latency, Kokoro warm first-audio
latency, and Opus encoding cost against local models only.  Nothing is
downloaded; when ``--wav-dir`` is omitted the scripted utterances are rendered
with Kokoro when the TTS model is configured, otherwise with the local
``espeak-ng`` binary.

Examples:
    SPEECH_MODEL_DIR=... TTS_MODEL_PATH=... TTS_VOICES_PATH=... \
        .venv/bin/python scripts/bench_speech.py --out speech-bench.json
    .venv/bin/python scripts/bench_speech.py --wav-dir ./wavs --skip-tts
"""

from __future__ import annotations

import argparse
import array
import asyncio
import json
import os
import platform
import re
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Any

from relay.config import Settings
from relay.speech.audio import OpusEncoder, PcmChunk, pcm16_from_float32
from relay.speech.backpressure import BufferLimits
from relay.speech.errors import SpeechError, SpeechModelUnavailable
from relay.speech.stt import SherpaStreamingStt
from relay.speech.tts import KokoroTtsEngine, StreamingTts

_HERE = Path(__file__).resolve().parent
_FRAME_MS = 20
_TOKEN_RE = re.compile(r"[a-z0-9']+")
_CPU_TONE_HZ = 440.0
_SILENCE_DBFS = -45.0
_MIN_TRAILING_SILENCE_MS = 20.0
_SILENCE_AMPLITUDE = round(32768 * 10 ** (_SILENCE_DBFS / 20.0))
_FINAL_PATHS = ("endpoint", "flush", "none")
_DECODER_LAG_STEP_MS = 40
_DECODER_LAG_MAX_MS = 480
# The benchmark measures synthesis latency, so it drains the whole stream
# instead of exercising the relay's drop-oldest backpressure policy.
_BENCH_TTS_LIMITS = BufferLimits(
    max_bytes=1_000_000,
    max_frames=4000,
    name="tts_audio_bench",
)


def _percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percent / 100.0
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "p50": None, "p95": None, "max": None, "mean": None}
    return {
        "count": len(values),
        "p50": _percentile(values, 50.0),
        "p95": _percentile(values, 95.0),
        "max": max(values),
        "mean": statistics.fmean(values),
    }


def _classify_final_path(*, endpoint: bool, flush: bool) -> str:
    """Return the acceptance-path label for one utterance."""
    if endpoint:
        return "endpoint"
    if flush:
        return "flush"
    return "none"


def _path_counts(paths: list[str]) -> dict[str, int]:
    """Count final-transcript paths, including zero-count entries."""
    counts = dict.fromkeys(_FINAL_PATHS, 0)
    for path in paths:
        counts[path] = counts.get(path, 0) + 1
    return counts


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _token_edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for row, reference_token in enumerate(reference, start=1):
        current = [row]
        for column, hypothesis_token in enumerate(hypothesis, start=1):
            substitution = previous[column - 1] + (reference_token != hypothesis_token)
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    substitution,
                )
            )
        previous = current
    return previous[-1]


def _word_error_rate(reference: str, hypothesis: str) -> float:
    reference_tokens = _tokens(reference)
    hypothesis_tokens = _tokens(hypothesis)
    if not reference_tokens:
        return 0.0 if not hypothesis_tokens else 1.0
    return _token_edit_distance(reference_tokens, hypothesis_tokens) / len(
        reference_tokens
    )


def _load_utterances(path: Path) -> list[str]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        items = payload.get("utterances", [])
    else:
        items = payload
    texts: list[str] = []
    for item in items:
        if isinstance(item, str):
            texts.append(item)
        elif isinstance(item, dict) and "text" in item:
            texts.append(str(item["text"]))
    if not texts:
        msg = f"no utterances found in {path}"
        raise SpeechError(msg)
    return texts


def _load_replies(path: Path) -> list[str]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        items = payload.get("replies", [])
    else:
        items = payload
    replies: list[str] = []
    for item in items:
        if isinstance(item, str):
            replies.append(item)
        elif isinstance(item, dict) and "text" in item:
            replies.append(str(item["text"]))
    if not replies:
        msg = f"no replies found in {path}"
        raise SpeechError(msg)
    return replies


def _write_wav(path: Path, chunk: PcmChunk) -> None:
    chunk.validate()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(chunk.sample_rate)
        handle.writeframes(chunk.data)


def _synthesize_espeak_wavs(texts: list[str], directory: Path) -> list[Path]:
    binary = shutil.which("espeak-ng")
    if binary is None:
        msg = (
            "espeak-ng is required to synthesize benchmark utterances when "
            "--wav-dir is omitted and Kokoro is not configured; install it "
            "locally, configure TTS_MODEL_PATH/TTS_VOICES_PATH, or pass --wav-dir"
        )
        raise SpeechError(msg)
    paths: list[Path] = []
    for index, text in enumerate(texts):
        path = directory / f"utterance-{index:02d}.wav"
        subprocess.run(
            [binary, "-w", str(path), "-s", "165", text],
            check=True,
            capture_output=True,
            text=True,
        )
        paths.append(path)
    return paths


async def _render_kokoro_wavs(
    texts: list[str],
    directory: Path,
    settings: Settings,
) -> list[Path]:
    engine = KokoroTtsEngine(settings.tts_model_path, settings.tts_voices_path)
    paths: list[Path] = []
    for index, text in enumerate(texts):
        chunk = await engine.synthesize(
            text,
            voice=settings.tts_voice,
            speed=settings.tts_speed,
        )
        path = directory / f"utterance-{index:02d}.wav"
        _write_wav(path, chunk)
        paths.append(path)
    return paths


def _synthesize_kokoro_wavs(
    texts: list[str],
    directory: Path,
    settings: Settings,
) -> list[Path]:
    return asyncio.run(_render_kokoro_wavs(texts, directory, settings))


def _kokoro_configured(settings: Settings) -> bool:
    return settings.tts_model_path is not None and settings.tts_voices_path is not None


def _prepare_utterance_wavs(
    texts: list[str],
    args: argparse.Namespace,
    settings: Settings,
    notes: list[str],
) -> tuple[list[Path], str]:
    if args.wav_dir is not None:
        wav_paths = sorted(args.wav_dir.glob("*.wav"))
        if not wav_paths:
            msg = f"no .wav files found in {args.wav_dir}"
            raise SpeechError(msg)
        notes.append(f"used {len(wav_paths)} pre-rendered wavs from {args.wav_dir}")
        return wav_paths, "wav-dir"

    directory = Path(tempfile.mkdtemp(prefix="hermes-talk-bench-"))

    if args.synth == "kokoro":
        if not _kokoro_configured(settings):
            msg = (
                "--synth kokoro requires TTS_MODEL_PATH and TTS_VOICES_PATH "
                "to point at local Kokoro model files"
            )
            raise SpeechModelUnavailable(msg)
        wav_paths = _synthesize_kokoro_wavs(texts, directory, settings)
        notes.append(
            f"synthesized {len(wav_paths)} utterances with Kokoro in {directory}"
        )
        return wav_paths, "kokoro"

    if args.synth == "auto" and _kokoro_configured(settings):
        try:
            wav_paths = _synthesize_kokoro_wavs(texts, directory, settings)
        except SpeechModelUnavailable as exc:
            notes.append(f"Kokoro synthesis unavailable ({exc}); using espeak-ng")
        else:
            notes.append(
                f"synthesized {len(wav_paths)} utterances with Kokoro in {directory}"
            )
            return wav_paths, "kokoro"

    wav_paths = _synthesize_espeak_wavs(texts, directory)
    notes.append(
        f"synthesized {len(wav_paths)} utterances with espeak-ng in {directory}"
    )
    if args.synth == "auto":
        notes.append(
            "espeak-ng speech produces near-garbage transcripts from this zipformer "
            "model (measured WER near 0.94); configure TTS_MODEL_PATH/TTS_VOICES_PATH "
            "or pass --synth kokoro for a meaningful word-error-rate column"
        )
    return wav_paths, "espeak-ng"


def _read_wav(path: Path) -> tuple[int, bytes]:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            msg = f"{path} must be mono PCM16"
            raise SpeechError(msg)
        return handle.getframerate(), handle.readframes(handle.getnframes())


def _samples_from_pcm(pcm: bytes) -> array.array[int]:
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _trim_trailing_silence(
    pcm: bytes,
    sample_rate: int,
) -> tuple[bytes, float, float]:
    """Trim a trailing silent run of at least 20 ms from PCM16 audio.

    Samples quieter than -45 dBFS are treated as silence.  Returns
    ``(trimmed_pcm, trimmed_trailing_silence_ms, speech_ms)``.
    """
    samples = _samples_from_pcm(pcm)
    last_speech = len(samples) - 1
    while last_speech >= 0 and abs(samples[last_speech]) < _SILENCE_AMPLITUDE:
        last_speech -= 1
    trailing_samples = len(samples) - (last_speech + 1)
    minimum_trailing = int(sample_rate * _MIN_TRAILING_SILENCE_MS / 1000.0)
    if trailing_samples < minimum_trailing:
        trailing_samples = 0
    keep_samples = len(samples) - trailing_samples
    trimmed_ms = trailing_samples / sample_rate * 1000.0
    speech_ms = keep_samples / sample_rate * 1000.0
    return pcm[: keep_samples * 2], trimmed_ms, speech_ms


def _gpu_line() -> str | None:
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used",
                "--format=csv,noheader",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if completed.returncode != 0:
        return None
    lines = completed.stdout.strip().splitlines()
    return lines[0] if lines else None


def _host_facts() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "gpu": _gpu_line(),
    }


def _select_model_name(root: Path, prefix: str) -> str | None:
    candidates = sorted(root.glob(f"{prefix}-*.onnx"))
    if not candidates:
        return None
    for candidate in candidates:
        if candidate.name.endswith(".int8.onnx"):
            return candidate.name
    return candidates[0].name


def _stt_model_info(model_dir: str | Path) -> dict[str, Any]:
    root = Path(model_dir).expanduser()
    return {
        "dir": root.name,
        "encoder": _select_model_name(root, "encoder"),
        "decoder": _select_model_name(root, "decoder"),
        "joiner": _select_model_name(root, "joiner"),
    }


def _tts_model_info(engine: KokoroTtsEngine, settings: Settings) -> dict[str, Any]:
    described = engine.describe()
    return {
        "engine": described.get("engine"),
        "model": described.get("model"),
        "voices": described.get("voices"),
        "voice": settings.tts_voice,
    }


def _peak_rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return usage / (1024.0 * 1024.0)
    return usage / 1024.0


def _bench_stt(
    adapter: SherpaStreamingStt,
    wav_paths: list[Path],
    utterances: list[str],
    repeat: int,
    silence_ms: int,
) -> dict[str, Any]:
    partial_counts: list[float] = []
    first_partial_ms: list[float] = []
    flush_final_ms: list[float] = []
    flush_decode_ms: list[float] = []
    flush_completeness: list[float] = []
    decoder_lag_padding_ms: list[float] = []
    endpoint_final_ms: list[float] = []
    endpoint_silence_ms: list[float] = []
    speech_ms: list[float] = []
    trimmed_trailing_silence_ms: list[float] = []
    word_error_rates: list[float] = []
    per_utterance: list[dict[str, Any]] = []

    async def feed_speech(
        session: Any,
        pcm: bytes,
        sample_rate: int,
    ) -> tuple[list[Any], float | None, bool, float]:
        frame_bytes = int(sample_rate * _FRAME_MS / 1000) * 2
        partials: list[Any] = []
        first_partial_at: float | None = None
        final_during_speech = False
        for offset in range(0, len(pcm), frame_bytes):
            data = pcm[offset : offset + frame_bytes]
            if not data:
                continue
            events = await session.feed(PcmChunk(data=data, sample_rate=sample_rate))
            for event in events:
                if event.kind == "partial":
                    partials.append(event)
                    if first_partial_at is None:
                        first_partial_at = time.perf_counter()
                else:
                    final_during_speech = True
        return partials, first_partial_at, final_during_speech, time.perf_counter()

    async def flush_attempt(
        pcm: bytes,
        sample_rate: int,
        wav_name: str,
    ) -> dict[str, Any] | None:
        session = await adapter.start_session()
        started = time.perf_counter()
        try:
            (
                partials,
                first_partial_at,
                final_during_speech,
                speech_end,
            ) = await feed_speech(session, pcm, sample_rate)
            if final_during_speech:
                return None
            flush_started = time.perf_counter()
            final = await session.flush()
            flush_finished = time.perf_counter()
        finally:
            await session.close()

        flush_decode_ms_value = (flush_finished - flush_started) * 1000.0
        if final is None or not final.text.strip():
            return {
                "partials": partials,
                "first_partial_ms": (
                    (first_partial_at - started) * 1000.0
                    if first_partial_at is not None
                    else None
                ),
                "flush_final_ms": None,
                "flush_decode_ms": flush_decode_ms_value,
                "transcript": "",
            }
        flush_final_ms_value = (flush_finished - speech_end) * 1000.0
        if flush_final_ms_value < 0:
            msg = (
                f"flush path for {wav_name} returned a final before the "
                "last speech frame was fed"
            )
            raise SpeechError(msg)
        return {
            "partials": partials,
            "first_partial_ms": (
                (first_partial_at - started) * 1000.0
                if first_partial_at is not None
                else None
            ),
            "flush_final_ms": flush_final_ms_value,
            "flush_decode_ms": flush_decode_ms_value,
            "transcript": final.text.strip(),
        }

    async def endpoint_attempt(
        pcm: bytes,
        sample_rate: int,
        wav_name: str,
    ) -> dict[str, Any] | None:
        session = await adapter.start_session()
        try:
            (
                partials,
                first_partial_at,
                final_during_speech,
                speech_end,
            ) = await feed_speech(session, pcm, sample_rate)
            if final_during_speech:
                return None
            frame_bytes = int(sample_rate * _FRAME_MS / 1000) * 2
            silence_chunk = PcmChunk(
                data=bytes(frame_bytes),
                sample_rate=sample_rate,
            )
            budget_frames = max(int(silence_ms // _FRAME_MS), 0)
            silence_fed_ms = 0.0
            endpoint_final: Any = None
            endpoint_at: float | None = None
            for _ in range(budget_frames):
                events = await session.feed(silence_chunk)
                silence_fed_ms += _FRAME_MS
                for event in events:
                    if event.kind == "final" and event.is_endpoint:
                        endpoint_final = event
                        endpoint_at = time.perf_counter()
                        break
                if endpoint_final is not None:
                    break
            transcript = ""
            if endpoint_final is not None:
                transcript = endpoint_final.text.strip()
            else:
                fallback_final = await session.flush()
                if fallback_final is not None:
                    transcript = fallback_final.text.strip()
        finally:
            await session.close()

        if endpoint_final is None or endpoint_at is None:
            return {
                "partials": partials,
                "first_partial_at": first_partial_at,
                "endpoint_final_ms": None,
                "endpoint_silence_ms": silence_fed_ms,
                "transcript": transcript,
            }
        endpoint_final_ms_value = (endpoint_at - speech_end) * 1000.0
        if endpoint_final_ms_value < 0:
            msg = (
                f"endpoint path for {wav_name} returned a final before the "
                "last speech frame was fed"
            )
            raise SpeechError(msg)
        return {
            "partials": partials,
            "first_partial_at": first_partial_at,
            "endpoint_final_ms": endpoint_final_ms_value,
            "endpoint_silence_ms": silence_fed_ms,
            "transcript": transcript,
        }

    async def lag_attempt(
        pcm: bytes,
        sample_rate: int,
        wav_name: str,
    ) -> dict[str, Any] | None:
        session = await adapter.start_session()
        try:
            (
                partials,
                _first_partial_at,
                final_during_speech,
                _speech_end,
            ) = await feed_speech(session, pcm, sample_rate)
            if final_during_speech:
                return None
            current_text = partials[-1].text if partials else ""
            transcripts: dict[int, str] = {0: current_text}
            silence_bytes = int(sample_rate * _DECODER_LAG_STEP_MS / 1000) * 2
            silence_chunk = PcmChunk(
                data=bytes(silence_bytes),
                sample_rate=sample_rate,
            )
            endpoint_text: str | None = None
            for padding_ms in range(
                _DECODER_LAG_STEP_MS,
                _DECODER_LAG_MAX_MS + 1,
                _DECODER_LAG_STEP_MS,
            ):
                events = await session.feed(silence_chunk)
                for event in events:
                    if event.kind == "final":
                        endpoint_text = event.text
                    else:
                        current_text = event.text
                if endpoint_text is not None:
                    current_text = endpoint_text
                    for remaining in range(
                        padding_ms,
                        _DECODER_LAG_MAX_MS + 1,
                        _DECODER_LAG_STEP_MS,
                    ):
                        transcripts[remaining] = current_text
                    break
                transcripts[padding_ms] = current_text
        finally:
            await session.close()

        reference = transcripts[_DECODER_LAG_MAX_MS]
        lag_ms = next(
            (
                padding_ms
                for padding_ms in sorted(transcripts)
                if transcripts[padding_ms] == reference
            ),
            _DECODER_LAG_MAX_MS,
        )
        return {
            "decoder_lag_padding_ms": lag_ms,
            "decoder_lag_transcripts": [
                {"padding_ms": padding_ms, "transcript": transcripts[padding_ms]}
                for padding_ms in sorted(transcripts)
            ],
            "decoder_lag_reference": reference,
        }

    async def run_with_retry(
        attempt: Any,
        pcm: bytes,
        sample_rate: int,
        wav_name: str,
        path_label: str,
    ) -> dict[str, Any]:
        for _ in range(2):
            result = await attempt(pcm, sample_rate, wav_name)
            if result is not None:
                return result
        msg = (
            f"{path_label} for {wav_name} produced a final transcript during the "
            "speech frames; retried once on a fresh session"
        )
        raise SpeechError(msg)

    async def run_one(wav_path: Path, scripted: str) -> dict[str, Any]:
        sample_rate, pcm = _read_wav(wav_path)
        trimmed_pcm, trimmed_ms, speech_ms_value = _trim_trailing_silence(
            pcm,
            sample_rate,
        )
        if not trimmed_pcm:
            msg = f"no speech samples remain after trimming {wav_path.name}"
            raise SpeechError(msg)
        flush_result = await run_with_retry(
            flush_attempt,
            trimmed_pcm,
            sample_rate,
            wav_path.name,
            "flush path",
        )
        endpoint_result = await run_with_retry(
            endpoint_attempt,
            trimmed_pcm,
            sample_rate,
            wav_path.name,
            "endpoint path",
        )
        flush_transcript = str(flush_result["transcript"]).strip()
        endpoint_transcript = str(endpoint_result["transcript"]).strip()
        transcript = endpoint_transcript or flush_transcript
        if not transcript:
            msg = f"no final transcript produced for {wav_path.name} on either path"
            raise SpeechError(msg)
        flush_completeness = _word_error_rate(scripted, flush_transcript)
        original_partial_interval = adapter.partial_interval_ms
        adapter.partial_interval_ms = 1
        try:
            lag_result = await run_with_retry(
                lag_attempt,
                trimmed_pcm,
                sample_rate,
                wav_path.name,
                "decoder lag probe",
            )
        finally:
            adapter.partial_interval_ms = original_partial_interval
        return {
            "wav": wav_path.name,
            "partial_count": len(flush_result["partials"]),
            "first_partial_ms": flush_result["first_partial_ms"],
            "trimmed_trailing_silence_ms": trimmed_ms,
            "speech_ms": speech_ms_value,
            "flush_final_ms": flush_result["flush_final_ms"],
            "flush_decode_ms": flush_result["flush_decode_ms"],
            "flush_transcript": flush_transcript,
            "flush_transcript_completeness": flush_completeness,
            "decoder_lag_padding_ms": lag_result["decoder_lag_padding_ms"],
            "decoder_lag_transcripts": lag_result["decoder_lag_transcripts"],
            "decoder_lag_reference": lag_result["decoder_lag_reference"],
            "endpoint_final_ms": endpoint_result["endpoint_final_ms"],
            "endpoint_silence_ms": endpoint_result["endpoint_silence_ms"],
            "endpoint_transcript": endpoint_transcript,
            "word_error_rate": _word_error_rate(scripted, transcript),
            "transcript": transcript,
        }

    for wav_path, scripted in zip(wav_paths, utterances, strict=False):
        for _ in range(repeat):
            result = asyncio.run(run_one(wav_path, scripted))
            per_utterance.append(result)
            partial_counts.append(float(result["partial_count"]))
            if result["first_partial_ms"] is not None:
                first_partial_ms.append(float(result["first_partial_ms"]))
            if result["flush_final_ms"] is not None:
                flush_final_ms.append(float(result["flush_final_ms"]))
            flush_decode_ms.append(float(result["flush_decode_ms"]))
            flush_completeness.append(float(result["flush_transcript_completeness"]))
            decoder_lag_padding_ms.append(float(result["decoder_lag_padding_ms"]))
            if result["endpoint_final_ms"] is not None:
                endpoint_final_ms.append(float(result["endpoint_final_ms"]))
            endpoint_silence_ms.append(float(result["endpoint_silence_ms"]))
            speech_ms.append(float(result["speech_ms"]))
            trimmed_trailing_silence_ms.append(
                float(result["trimmed_trailing_silence_ms"])
            )
            word_error_rates.append(float(result["word_error_rate"]))

    return {
        "partial_counts": _summary(partial_counts),
        "first_partial_ms": _summary(first_partial_ms),
        "flush_final_ms": _summary(flush_final_ms),
        "flush_decode_ms": _summary(flush_decode_ms),
        "flush_transcript_completeness": _summary(flush_completeness),
        "decoder_lag_padding_ms": _summary(decoder_lag_padding_ms),
        "endpoint_final_ms": _summary(endpoint_final_ms),
        "endpoint_silence_ms": _summary(endpoint_silence_ms),
        "speech_ms": _summary(speech_ms),
        "trimmed_trailing_silence_ms": _summary(trimmed_trailing_silence_ms),
        "word_error_rate": _summary(word_error_rates),
        "per_utterance": per_utterance,
    }


async def _warmup_tts(engine: KokoroTtsEngine, settings: Settings) -> float:
    tts = StreamingTts(
        engine,
        voice=settings.tts_voice,
        speed=settings.tts_speed,
        opus_bit_rate=settings.opus_bit_rate,
        max_pending_sentences=settings.tts_max_pending_sentences,
        backpressure_timeout_s=settings.speech_backpressure_timeout_s,
        first_segment_max_chars=settings.tts_first_segment_max_chars,
    )
    try:
        return await tts.warmup()
    finally:
        await tts.close()


def _bench_tts(
    model_path: Path,
    voices_path: Path,
    short_replies: list[str],
    long_replies: list[str],
    settings: Settings,
    repeat: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    cold_started = time.perf_counter()
    engine = KokoroTtsEngine(model_path, voices_path)
    warmup_ms = asyncio.run(_warmup_tts(engine, settings))
    cold_start_ms = (time.perf_counter() - cold_started) * 1000.0
    model_info = _tts_model_info(engine, settings)

    async def run_one(reply: str, first_segment_max_chars: int) -> dict[str, Any]:
        tts = StreamingTts(
            engine,
            voice=settings.tts_voice,
            speed=settings.tts_speed,
            opus_bit_rate=settings.opus_bit_rate,
            max_pending_sentences=settings.tts_max_pending_sentences,
            backpressure_timeout_s=settings.speech_backpressure_timeout_s,
            first_segment_max_chars=first_segment_max_chars,
            limits=_BENCH_TTS_LIMITS,
        )
        started = time.perf_counter()
        first_at: float | None = None
        first_segment_chars: int | None = None
        packets = 0
        audio_ms = 0.0
        last_at = started
        try:
            await tts.push_text(reply)
            await tts.flush()
            async for chunk in tts.chunks():
                now = time.perf_counter()
                if first_at is None:
                    first_at = now
                    first_segment_chars = len(chunk.text)
                last_at = now
                packets += len(chunk.opus_packets)
                audio_ms += chunk.duration_ms
            metrics = tts.metrics()
        finally:
            await tts.close()
        elapsed_s = last_at - started
        audio_s = audio_ms / 1000.0
        return {
            "reply": reply,
            "first_packet_ms": (
                (first_at - started) * 1000.0 if first_at is not None else None
            ),
            "first_segment_chars": first_segment_chars,
            "last_packet_ms": elapsed_s * 1000.0,
            "packets": packets,
            "audio_ms": audio_ms,
            "rtf": elapsed_s / audio_s if audio_s > 0 else None,
            "metrics_first_audio_ms": list(metrics.first_audio_ms),
            "metrics_sentences": metrics.sentences,
            "metrics_packets": metrics.packets,
        }

    def run_group(
        replies: list[str],
        first_segment_max_chars: int,
        group_repeat: int,
    ) -> dict[str, Any]:
        first_packet_ms: list[float] = []
        first_segment_chars: list[float] = []
        last_packet_ms: list[float] = []
        rtf_values: list[float] = []
        packet_counts: list[float] = []
        metric_first_audio: list[float] = []
        per_reply: list[dict[str, Any]] = []
        for reply in replies:
            for _ in range(group_repeat):
                result = asyncio.run(
                    run_one(reply, first_segment_max_chars),
                )
                per_reply.append(result)
                if result["first_packet_ms"] is not None:
                    first_packet_ms.append(float(result["first_packet_ms"]))
                if result["first_segment_chars"] is not None:
                    first_segment_chars.append(float(result["first_segment_chars"]))
                last_packet_ms.append(float(result["last_packet_ms"]))
                if result["rtf"] is not None:
                    rtf_values.append(float(result["rtf"]))
                packet_counts.append(float(result["packets"]))
                metric_first_audio.extend(
                    float(value) for value in result["metrics_first_audio_ms"]
                )
        return {
            "first_packet_ms": _summary(first_packet_ms),
            "first_segment_chars": _summary(first_segment_chars),
            "last_packet_ms": _summary(last_packet_ms),
            "rtf": _summary(rtf_values),
            "packets": _summary(packet_counts),
            "metrics_first_audio_ms": _summary(metric_first_audio),
            "per_reply": per_reply,
        }

    capped_chars = settings.tts_first_segment_max_chars
    short_report = run_group(short_replies, capped_chars, repeat)
    long_report = run_group(long_replies, capped_chars, repeat)
    uncapped_report = run_group(long_replies, 400, 1)

    warm_summary = short_report["first_packet_ms"]
    warm_p50 = warm_summary.get("p50")
    cold_first_audio_ms = (
        cold_start_ms + float(warm_p50) if warm_p50 is not None else None
    )
    report = {
        "cold_start_ms": cold_start_ms,
        "warmup_ms": warmup_ms,
        "cold_first_audio_ms": cold_first_audio_ms,
        "first_packet_ms": warm_summary,
        "warm_first_packet_ms": warm_summary,
        "last_packet_ms": short_report["last_packet_ms"],
        "rtf": short_report["rtf"],
        "packets": short_report["packets"],
        "metrics_first_audio_ms": short_report["metrics_first_audio_ms"],
        "per_reply": short_report["per_reply"],
        "groups": {"short": short_report, "long": long_report},
        "long_uncapped_first_packet_ms": uncapped_report["first_packet_ms"],
        "long_uncapped_first_segment_chars": uncapped_report["first_segment_chars"],
        "long_uncapped_per_reply": uncapped_report["per_reply"],
    }
    return report, model_info


def _bench_opus() -> dict[str, Any]:
    import numpy as np

    frames = 24000
    tone = np.sin(2.0 * np.pi * _CPU_TONE_HZ * np.arange(frames) / 24000.0)
    chunk = pcm16_from_float32(tone.astype(np.float32) * 0.25, 24000)
    encoder = OpusEncoder(bit_rate=32000)
    started = time.perf_counter()
    try:
        packets = encoder.encode(chunk)
        packets.extend(encoder.flush())
    finally:
        encoder.close()
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return {
        "encode_ms": elapsed_ms,
        "packets": len(packets),
        "audio_ms": chunk.duration_ms,
        "rtf": elapsed_ms / chunk.duration_ms,
    }


def _print_table(report: dict[str, Any]) -> None:
    rows: list[tuple[str, str, dict[str, Any]]] = []
    for stage in ("stt", "tts"):
        for metric, summary in report.get(stage, {}).items():
            if isinstance(summary, dict) and "p50" in summary:
                rows.append((stage, metric, summary))
    opus = report.get("opus")
    if isinstance(opus, dict):
        rows.append(
            (
                "opus",
                "encode_ms",
                {
                    "count": 1,
                    "p50": opus["encode_ms"],
                    "p95": opus["encode_ms"],
                    "max": opus["encode_ms"],
                },
            )
        )
    if not rows:
        print("bench_speech: no stages were run")
        return
    print(f"{'stage':<6} {'metric':<24} {'p50':>10} {'p95':>10} {'max':>10}")
    for stage, metric, summary in rows:
        p50 = summary.get("p50")
        p95 = summary.get("p95")
        maximum = summary.get("max")
        print(
            f"{stage:<6} {metric:<24} "
            f"{_fmt(p50):>10} {_fmt(p95):>10} {_fmt(maximum):>10}"
        )


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}"


def _print_extras(report: dict[str, Any]) -> None:
    stt = report.get("stt")
    if isinstance(stt, dict):
        flush = stt.get("flush_final_ms")
        if isinstance(flush, dict):
            print(
                "bench_speech: stt padded flush (acceptance metric) "
                "flush_final_ms "
                f"count={flush.get('count')} "
                f"p50={_fmt(flush.get('p50'))} "
                f"p95={_fmt(flush.get('p95'))} "
                f"max={_fmt(flush.get('max'))}"
            )
        flush_decode = stt.get("flush_decode_ms")
        if isinstance(flush_decode, dict):
            print(
                "bench_speech: stt padded flush flush_decode_ms "
                f"count={flush_decode.get('count')} "
                f"p50={_fmt(flush_decode.get('p50'))} "
                f"p95={_fmt(flush_decode.get('p95'))} "
                f"max={_fmt(flush_decode.get('max'))}"
            )
        completeness = stt.get("flush_transcript_completeness")
        if isinstance(completeness, dict):
            print(
                "bench_speech: stt padded flush flush_transcript_completeness (WER) "
                f"count={completeness.get('count')} "
                f"p50={_fmt(completeness.get('p50'))} "
                f"p95={_fmt(completeness.get('p95'))} "
                f"max={_fmt(completeness.get('max'))}"
            )
        decoder_lag = stt.get("decoder_lag_padding_ms")
        if isinstance(decoder_lag, dict):
            print(
                "bench_speech: stt decoder_lag_padding_ms "
                f"count={decoder_lag.get('count')} "
                f"p50={_fmt(decoder_lag.get('p50'))} "
                f"p95={_fmt(decoder_lag.get('p95'))} "
                f"max={_fmt(decoder_lag.get('max'))}"
            )
        endpoint = stt.get("endpoint_final_ms")
        if isinstance(endpoint, dict):
            print(
                "bench_speech: stt endpoint path (VAD-policy latency, not compute) "
                "endpoint_final_ms "
                f"count={endpoint.get('count')} "
                f"p50={_fmt(endpoint.get('p50'))} "
                f"p95={_fmt(endpoint.get('p95'))} "
                f"max={_fmt(endpoint.get('max'))}"
            )
        endpoint_silence = stt.get("endpoint_silence_ms")
        if isinstance(endpoint_silence, dict):
            print(
                "bench_speech: stt endpoint path endpoint_silence_ms "
                f"count={endpoint_silence.get('count')} "
                f"p50={_fmt(endpoint_silence.get('p50'))} "
                f"p95={_fmt(endpoint_silence.get('p95'))} "
                f"max={_fmt(endpoint_silence.get('max'))}"
            )
    tts = report.get("tts")
    if isinstance(tts, dict):
        cold_start = tts.get("cold_start_ms")
        warm = tts.get("warm_first_packet_ms")
        warm_p50 = warm.get("p50") if isinstance(warm, dict) else None
        if cold_start is not None and warm_p50 is not None:
            total = float(cold_start) + float(warm_p50)
            print(
                f"bench_speech: tts cold_start_ms={float(cold_start):.2f} "
                f"warm_first_packet_ms_p50={float(warm_p50):.2f} "
                f"total_first_audio_from_process_start_ms={total:.2f}"
            )
        elif cold_start is not None:
            print(f"bench_speech: tts cold_start_ms={float(cold_start):.2f}")
        groups = tts.get("groups")
        if isinstance(groups, dict):
            for group_name in ("short", "long"):
                group = groups.get(group_name)
                if not isinstance(group, dict):
                    continue
                first_packet = group.get("first_packet_ms")
                first_chars = group.get("first_segment_chars")
                if isinstance(first_packet, dict):
                    print(
                        f"bench_speech: tts group={group_name} first_packet_ms "
                        f"p50={_fmt(first_packet.get('p50'))} "
                        f"p95={_fmt(first_packet.get('p95'))} "
                        f"max={_fmt(first_packet.get('max'))}"
                    )
                if isinstance(first_chars, dict):
                    print(
                        f"bench_speech: tts group={group_name} "
                        f"mean_first_segment_chars={_fmt(first_chars.get('mean'))}"
                    )
        uncapped = tts.get("long_uncapped_first_packet_ms")
        if isinstance(uncapped, dict):
            print(
                "bench_speech: tts long_uncapped_first_packet_ms "
                f"p50={_fmt(uncapped.get('p50'))} "
                f"p95={_fmt(uncapped.get('p95'))} "
                f"max={_fmt(uncapped.get('max'))}"
            )
    synth_source = report.get("synth_source")
    if synth_source:
        print(f"bench_speech: synth_source={synth_source}")
    stt_model = report.get("stt_model")
    if isinstance(stt_model, dict):
        print(
            "bench_speech: stt_model dir={} encoder={} decoder={} joiner={}".format(
                stt_model.get("dir"),
                stt_model.get("encoder"),
                stt_model.get("decoder"),
                stt_model.get("joiner"),
            )
        )
    tts_model = report.get("tts_model")
    if isinstance(tts_model, dict):
        print(
            "bench_speech: tts_model engine={} model={} voices={} voice={}".format(
                tts_model.get("engine"),
                tts_model.get("model"),
                tts_model.get("voices"),
                tts_model.get("voice"),
            )
        )
    host = report.get("host")
    if isinstance(host, dict):
        print(
            "bench_speech: host platform={} cpu_count={} gpu={}".format(
                host.get("platform"),
                host.get("cpu_count"),
                host.get("gpu"),
            )
        )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-dir",
        default=os.environ.get("SPEECH_MODEL_DIR"),
        help="sherpa-onnx model directory (default: SPEECH_MODEL_DIR)",
    )
    parser.add_argument("--wav-dir", type=Path, help="directory of mono PCM16 wavs")
    parser.add_argument(
        "--utterances",
        type=Path,
        default=_HERE / "voice_bench_utterances.json",
        help="scripted utterances JSON",
    )
    parser.add_argument(
        "--replies",
        type=Path,
        default=_HERE / "speech_bench_replies.json",
        help="reply sentences JSON",
    )
    parser.add_argument(
        "--long-replies",
        type=Path,
        default=_HERE / "speech_bench_long_openers.json",
        help="long opener replies JSON for the capped/uncapped TTS comparison",
    )
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument(
        "--silence-ms",
        type=int,
        default=1200,
        help="trailing silence budget fed after speech to measure endpoint latency",
    )
    parser.add_argument(
        "--synth",
        choices=("auto", "kokoro", "espeak"),
        default="auto",
        help="utterance synthesis source (default: Kokoro when configured, else espeak-ng)",
    )
    parser.add_argument("--out", type=Path, help="write the full JSON report here")
    parser.add_argument("--skip-stt", action="store_true")
    parser.add_argument("--skip-tts", action="store_true")
    parser.add_argument("--skip-opus", action="store_true")
    return parser.parse_args(argv)


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.silence_ms < 0:
        msg = "--silence-ms must be zero or greater"
        raise SpeechError(msg)
    settings = Settings(_env_file=None)
    notes: list[str] = []
    report: dict[str, Any] = {
        "config": {
            "repeat": args.repeat,
            "model_dir": str(args.model_dir) if args.model_dir else None,
            "wav_dir": str(args.wav_dir) if args.wav_dir else None,
            "silence_ms": args.silence_ms,
            "synth": args.synth,
            "stt_flush_padding_ms": settings.stt_flush_padding_ms,
        },
        "notes": notes,
    }

    utterances = _load_utterances(args.utterances)
    synth_source: str | None = None

    if not args.skip_stt:
        if not args.model_dir:
            msg = (
                "sherpa-onnx model directory is not configured: pass --model-dir "
                "or set SPEECH_MODEL_DIR"
            )
            raise SpeechModelUnavailable(msg)
        wav_paths, synth_source = _prepare_utterance_wavs(
            utterances,
            args,
            settings,
            notes,
        )
        adapter = SherpaStreamingStt(
            Path(args.model_dir),
            num_threads=settings.stt_num_threads,
            partial_interval_ms=settings.stt_partial_interval_ms,
            flush_padding_ms=settings.stt_flush_padding_ms,
        )
        report["stt_model"] = _stt_model_info(args.model_dir)
        report["stt"] = _bench_stt(
            adapter,
            wav_paths,
            utterances,
            max(args.repeat, 1),
            args.silence_ms,
        )

    if not args.skip_tts:
        model_path = settings.tts_model_path
        voices_path = settings.tts_voices_path
        if model_path is None or voices_path is None:
            msg = "Kokoro TTS is not configured: set TTS_MODEL_PATH and TTS_VOICES_PATH"
            raise SpeechModelUnavailable(msg)
        replies = _load_replies(args.replies)
        long_replies = _load_replies(args.long_replies)
        tts_report, tts_model_info = _bench_tts(
            model_path,
            voices_path,
            replies,
            long_replies,
            settings,
            max(args.repeat, 1),
        )
        report["tts"] = tts_report
        report["tts_model"] = tts_model_info

    if not args.skip_opus:
        report["opus"] = _bench_opus()

    report["synth_source"] = synth_source
    report["host"] = _host_facts()
    report["resources"] = {
        "cpu_time_s": time.process_time(),
        "peak_rss_mb": _peak_rss_mb(),
        "gpu_memory": _gpu_line(),
    }
    return report


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = _run(args)
    except (SpeechError, ImportError) as exc:
        print(f"bench_speech: error: {exc}", file=sys.stderr)
        return 1

    if args.out is not None:
        args.out.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"bench_speech: wrote {args.out}")

    for note in report.get("notes", []):
        print(f"bench_speech: {note}")
    _print_table(report)
    _print_extras(report)
    resources = report.get("resources", {})
    print(
        "bench_speech: cpu_time_s={:.2f} peak_rss_mb={:.1f}".format(
            resources.get("cpu_time_s", 0.0),
            resources.get("peak_rss_mb", 0.0),
        )
    )
    gpu = resources.get("gpu_memory")
    if gpu:
        print(f"bench_speech: gpu_memory={gpu}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
