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
from relay.speech.errors import SpeechError, SpeechModelUnavailable
from relay.speech.stt import SherpaStreamingStt
from relay.speech.tts import KokoroTtsEngine, StreamingTts

_HERE = Path(__file__).resolve().parent
_FRAME_MS = 20
_TOKEN_RE = re.compile(r"[a-z0-9']+")
_CPU_TONE_HZ = 440.0
_FINAL_PATHS = ("endpoint", "flush", "none")


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
    end_of_speech_to_final_ms: list[float] = []
    flush_decode_ms: list[float] = []
    trailing_silence_ms: list[float] = []
    final_paths: list[str] = []
    word_error_rates: list[float] = []
    per_utterance: list[dict[str, Any]] = []

    async def run_one(wav_path: Path, scripted: str) -> dict[str, Any]:
        sample_rate, pcm = _read_wav(wav_path)
        session = await adapter.start_session()
        partials: list[Any] = []
        finals: list[Any] = []
        frame_bytes = int(sample_rate * _FRAME_MS / 1000) * 2
        first_partial_at: float | None = None
        speech_final: Any = None
        speech_final_at: float | None = None
        started = time.perf_counter()
        try:
            for offset in range(0, len(pcm), frame_bytes):
                chunk = PcmChunk(
                    data=pcm[offset : offset + frame_bytes],
                    sample_rate=sample_rate,
                )
                if not chunk.data:
                    continue
                events = await session.feed(chunk)
                for event in events:
                    if event.kind == "partial":
                        partials.append(event)
                        if first_partial_at is None:
                            first_partial_at = time.perf_counter()
                    else:
                        finals.append(event)
                        if event.is_endpoint:
                            speech_final = event
                            speech_final_at = time.perf_counter()
            speech_end = time.perf_counter()

            endpoint_final: Any = None
            endpoint_at: float | None = None
            trailing_ms = 0.0
            silence_chunk = PcmChunk(
                data=bytes(frame_bytes),
                sample_rate=sample_rate,
            )
            budget_frames = max(int(silence_ms // _FRAME_MS), 0)
            for _ in range(budget_frames):
                events = await session.feed(silence_chunk)
                trailing_ms += _FRAME_MS
                for event in events:
                    if event.kind == "final":
                        finals.append(event)
                        if event.is_endpoint and endpoint_final is None:
                            endpoint_final = event
                            endpoint_at = time.perf_counter()
                if endpoint_final is not None:
                    break

            flush_final: Any = None
            flush_decode: float | None = None
            if endpoint_final is not None:
                final_path = _classify_final_path(endpoint=True, flush=False)
                end_to_final = (endpoint_at - speech_end) * 1000.0  # type: ignore[operator]
            else:
                flush_started = time.perf_counter()
                flush_final = await session.flush()
                flush_decode = (time.perf_counter() - flush_started) * 1000.0
                if flush_final is not None:
                    finals.append(flush_final)
                    final_path = _classify_final_path(endpoint=False, flush=True)
                    end_to_final = (time.perf_counter() - speech_end) * 1000.0
                elif speech_final is not None:
                    final_path = _classify_final_path(endpoint=True, flush=False)
                    end_to_final = (
                        max(0.0, (speech_final_at - speech_end) * 1000.0)
                        if speech_final_at is not None
                        else 0.0
                    )
                else:
                    final_path = _classify_final_path(endpoint=False, flush=False)
                    end_to_final = None
        finally:
            await session.close()

        transcript = " ".join(event.text for event in finals if event.text).strip()
        if not transcript:
            msg = (
                f"no final transcript produced for {wav_path.name} "
                f"(speech={len(pcm) / sample_rate:.2f}s, silence_budget={silence_ms}ms)"
            )
            raise SpeechError(msg)
        return {
            "wav": wav_path.name,
            "partial_count": len(partials),
            "first_partial_ms": (
                (first_partial_at - started) * 1000.0
                if first_partial_at is not None
                else None
            ),
            "end_of_speech_to_final_ms": end_to_final,
            "flush_decode_ms": flush_decode,
            "trailing_silence_ms": trailing_ms,
            "final_path": final_path,
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
            if result["end_of_speech_to_final_ms"] is not None:
                end_of_speech_to_final_ms.append(
                    float(result["end_of_speech_to_final_ms"])
                )
            if result["flush_decode_ms"] is not None:
                flush_decode_ms.append(float(result["flush_decode_ms"]))
            trailing_silence_ms.append(float(result["trailing_silence_ms"]))
            final_paths.append(str(result["final_path"]))
            word_error_rates.append(float(result["word_error_rate"]))

    return {
        "partial_counts": _summary(partial_counts),
        "first_partial_ms": _summary(first_partial_ms),
        "end_of_speech_to_final_ms": _summary(end_of_speech_to_final_ms),
        "flush_decode_ms": _summary(flush_decode_ms),
        "trailing_silence_ms": _summary(trailing_silence_ms),
        "final_paths": _path_counts(final_paths),
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
    )
    try:
        return await tts.warmup()
    finally:
        await tts.close()


def _bench_tts(
    model_path: Path,
    voices_path: Path,
    replies: list[str],
    settings: Settings,
    repeat: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    cold_started = time.perf_counter()
    engine = KokoroTtsEngine(model_path, voices_path)
    warmup_ms = asyncio.run(_warmup_tts(engine, settings))
    cold_start_ms = (time.perf_counter() - cold_started) * 1000.0
    model_info = _tts_model_info(engine, settings)

    first_packet_ms: list[float] = []
    last_packet_ms: list[float] = []
    rtf_values: list[float] = []
    packet_counts: list[float] = []
    metric_first_audio: list[float] = []
    per_reply: list[dict[str, Any]] = []

    async def run_one(reply: str) -> dict[str, Any]:
        tts = StreamingTts(
            engine,
            voice=settings.tts_voice,
            speed=settings.tts_speed,
            opus_bit_rate=settings.opus_bit_rate,
            max_pending_sentences=settings.tts_max_pending_sentences,
            backpressure_timeout_s=settings.speech_backpressure_timeout_s,
        )
        started = time.perf_counter()
        first_at: float | None = None
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
            "last_packet_ms": elapsed_s * 1000.0,
            "packets": packets,
            "audio_ms": audio_ms,
            "rtf": elapsed_s / audio_s if audio_s > 0 else None,
            "metrics_first_audio_ms": list(metrics.first_audio_ms),
            "metrics_sentences": metrics.sentences,
            "metrics_packets": metrics.packets,
        }

    for reply in replies:
        for _ in range(repeat):
            result = asyncio.run(run_one(reply))
            per_reply.append(result)
            if result["first_packet_ms"] is not None:
                first_packet_ms.append(float(result["first_packet_ms"]))
            last_packet_ms.append(float(result["last_packet_ms"]))
            if result["rtf"] is not None:
                rtf_values.append(float(result["rtf"]))
            packet_counts.append(float(result["packets"]))
            metric_first_audio.extend(
                float(value) for value in result["metrics_first_audio_ms"]
            )

    warm_summary = _summary(first_packet_ms)
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
        "last_packet_ms": _summary(last_packet_ms),
        "rtf": _summary(rtf_values),
        "packets": _summary(packet_counts),
        "metrics_first_audio_ms": _summary(metric_first_audio),
        "per_reply": per_reply,
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
        paths = stt.get("final_paths")
        if isinstance(paths, dict):
            counts = ", ".join(f"{name}={paths.get(name, 0)}" for name in _FINAL_PATHS)
            print(f"bench_speech: stt final_paths: {counts}")
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
        tts_report, tts_model_info = _bench_tts(
            model_path,
            voices_path,
            replies,
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
