#!/usr/bin/env python3
"""End-to-end latency benchmark for the shipped local voice pipeline.

This script replays scripted utterances through the real relay classes and
measures each stage of the path:

1. Utterance rendering with the local Kokoro engine (or a pre-rendered mono
   PCM16 wav from ``--wav-dir``).
2. Streaming speech-to-text with the local sherpa-onnx transducer.
3. Voice-brain routing against a local OpenAI-compatible llama.cpp endpoint.
4. Streaming Kokoro text-to-speech and Opus packetization.

Nothing contacts a cloud service.  STT and TTS both run on CPU onnxruntime;
only the router talks to a local llama.cpp HTTP endpoint (loopback by default).

Definitions used in the report
------------------------------
``perceived_ms``
    The perceived response time: from the moment the user stops speaking
    (``t_end_speech``, after the last non-silent frame was fed to STT) to the
    first Opus audio packet leaving the pipeline (``t_first_audio``).

``turn_commit_ms``
    The deliberate end-of-turn window.  After the speech audio is exhausted,
    the harness feeds this many milliseconds of digital silence in 20 ms
    frames before flushing STT.  The default 600 ms mirrors the shipped
    ``EndOfTurnConfig.silence_commit_ms`` in ``relay/transport/turns.py``.
    That module is intentionally not imported: it is optional/uncommitted work
    and the value is passed as a CLI flag instead.

The report never contains absolute ``/home`` paths: model paths are reduced to
basenames, and diagnostic text has the home directory collapsed to ``~``.

Examples:
    SPEECH_MODEL_DIR=... TTS_MODEL_PATH=... TTS_VOICES_PATH=... \\
        .venv/bin/python relay/tests/bench_latency.py --json-out latency.json
    .venv/bin/python relay/tests/bench_latency.py --wav-dir ./wavs \\
        --compare baseline.json --regression-pct 10
"""

from __future__ import annotations

import argparse
import array
import asyncio
import hashlib
import ipaddress
import json
import os
import platform
import re
import select
import shutil
import statistics
import subprocess
import sys
import threading
import time
import wave
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from relay.brain.router import (  # noqa: E402
    RouterConfig,
    RouterError,
    RouterTimeout,
    RouterUnavailable,
    VoiceBrainRouter,
)
from relay.config import Settings  # noqa: E402
from relay.speech.audio import PcmChunk  # noqa: E402
from relay.speech.backpressure import BufferLimits  # noqa: E402
from relay.speech.errors import SpeechError, SpeechModelUnavailable  # noqa: E402
from relay.speech.stt import SherpaStreamingStt  # noqa: E402
from relay.speech.tts import KokoroTtsEngine, StreamingTts  # noqa: E402

_FRAME_MS = 20
_STT_SAMPLE_RATE = 16000
_SILENCE_DBFS = -45.0
_MIN_TRAILING_SILENCE_MS = 20.0
_SILENCE_AMPLITUDE = round(32768 * 10 ** (_SILENCE_DBFS / 20.0))
_TOKEN_RE = re.compile(r"[a-z0-9']+")
_NVIDIA_SMI = shutil.which("nvidia-smi")
_GIT = shutil.which("git")
_SCHEMA_VERSION = 1
_TABLE_METRICS = (
    "turn_commit_ms",
    "stt_finalize_ms",
    "router_first_spoken_ms",
    "tts_first_audio_ms",
    "e2e_compute_ms",
    "perceived_ms",
    "reply_complete_ms",
    "audio_ms",
)
_NUMERIC_METRICS = (
    "turn_commit_ms",
    "stt_finalize_ms",
    "router_first_spoken_ms",
    "tts_first_audio_ms",
    "e2e_compute_ms",
    "perceived_ms",
    "reply_complete_ms",
    "audio_ms",
    "packets",
    "router_ttft_ms",
    "router_total_ms",
    "word_error_rate",
)
_TOOL_METRICS = (
    "perceived_ms",
    "e2e_compute_ms",
    "tts_first_audio_ms",
    "reply_complete_ms",
)
_COMPARE_STATS = ("p50", "p95")
_COMPARABLE_CONFIG_PATHS = (
    ("utterance_set_sha256",),
    ("turn_commit_ms",),
    ("stt", "model"),
    ("tts", "model"),
    ("router", "model"),
    ("tts", "voice"),
    ("repeat",),
)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested offline by relay/tests/test_bench_latency.py)
# ---------------------------------------------------------------------------


def _percentile(values: list[float], percent: float) -> float | None:
    """Return a linearly interpolated percentile, or ``None`` for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percent / 100.0
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _summary(values: list[float]) -> dict[str, float | int | None]:
    """Summarize a metric series with the report's fixed shape."""
    if not values:
        return {"count": 0, "p50": None, "p95": None, "max": None, "mean": None}
    return {
        "count": len(values),
        "p50": _percentile(values, 50.0),
        "p95": _percentile(values, 95.0),
        "max": max(values),
        "mean": statistics.fmean(values),
    }


def _acceptance(perceived_p50_ms: float | None, threshold_ms: float) -> dict[str, Any]:
    """Evaluate the acceptance criterion strictly below ``threshold_ms``."""
    met = perceived_p50_ms is not None and perceived_p50_ms < threshold_ms
    return {
        "perceived_p50_ms": perceived_p50_ms,
        "threshold_ms": threshold_ms,
        "met": met,
    }


def _compare_summaries(
    baseline: dict[str, Any],
    current: dict[str, Any],
    regression_pct: float,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Compare two stage-summary maps and flag percent regressions.

    The result maps each metric to ``p50``/``p95`` entries containing the
    baseline and current values, absolute and percent deltas, and a
    ``regression`` flag for deltas above ``regression_pct``.
    """
    comparison: dict[str, dict[str, dict[str, Any]]] = {}
    for metric in sorted(set(baseline) | set(current)):
        if not metric.endswith("_ms") or metric == "audio_ms":
            continue
        metric_entry: dict[str, dict[str, Any]] = {}
        baseline_metric = baseline.get(metric) or {}
        current_metric = current.get(metric) or {}
        for stat in _COMPARE_STATS:
            baseline_value = baseline_metric.get(stat)
            current_value = current_metric.get(stat)
            delta_ms: float | None = None
            delta_pct: float | None = None
            regression = False
            if baseline_value is not None and current_value is not None:
                delta_ms = float(current_value) - float(baseline_value)
                if float(baseline_value) > 0:
                    delta_pct = delta_ms / float(baseline_value) * 100.0
                    regression = delta_pct > regression_pct
            metric_entry[stat] = {
                "baseline": baseline_value,
                "current": current_value,
                "delta_ms": delta_ms,
                "delta_pct": delta_pct,
                "regression": regression,
            }
        comparison[metric] = metric_entry
    return comparison


def _utterance_fingerprint(cases: list[dict[str, Any]]) -> str:
    """Return a stable SHA-256 over the scripted case list."""
    canonical = json.dumps(cases, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    """Return token-level word error rate against the scripted text."""
    reference_tokens = _tokens(reference)
    hypothesis_tokens = _tokens(hypothesis)
    if not reference_tokens:
        return 0.0 if not hypothesis_tokens else 1.0
    return _token_edit_distance(reference_tokens, hypothesis_tokens) / len(reference_tokens)


# ---------------------------------------------------------------------------
# Small report/CLI helpers
# ---------------------------------------------------------------------------


def _basename(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    return Path(raw).name if raw else None


def _scrub(text: str) -> str:
    """Collapse the user directory so reports never contain absolute home paths."""
    home = os.path.expanduser("~")
    return text.replace(home, "~") if home else text


def _config_value(config: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = config
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _comparability(
    baseline_config: dict[str, Any], current_config: dict[str, Any]
) -> dict[str, Any]:
    mismatches: list[str] = []
    for path in _COMPARABLE_CONFIG_PATHS:
        label = ".".join(path)
        baseline_value = _config_value(baseline_config, path)
        current_value = _config_value(current_config, path)
        if path[-1] == "model":
            baseline_value = _basename(baseline_value)
            current_value = _basename(current_value)
        if baseline_value != current_value:
            mismatches.append(f"{label}: baseline={baseline_value!r} current={current_value!r}")
    return {"comparable": not mismatches, "mismatches": mismatches}


def _fmt_ms(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.1f}"


def _fmt_delta(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):+.1f}"


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):+.1f}%"


def _parse_number(value: str) -> int | None:
    raw = value.strip()
    if not raw or raw.upper() in {"N/A", "[N/A]"}:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _read_proc_stat() -> tuple[int, int] | None:
    try:
        first = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return None
    parts = first.split()
    if not parts or parts[0] != "cpu":
        return None
    try:
        values = [int(part) for part in parts[1:]]
    except ValueError:
        return None
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return max(0, total - idle), total


def _proc_cpu_seconds() -> float | None:
    try:
        raw = Path("/proc/self/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    end = raw.rfind(")")
    if end < 0:
        return None
    fields = raw[end + 2 :].split()
    if len(fields) < 13:
        return None
    try:
        utime = int(fields[11])
        stime = int(fields[12])
        ticks = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        return None
    if ticks <= 0:
        ticks = 100
    return (utime + stime) / ticks


def _read_loadavg() -> list[float] | None:
    try:
        parts = Path("/proc/loadavg").read_text(encoding="utf-8").split()[:3]
        return [float(part) for part in parts]
    except (OSError, ValueError):
        return None


def _run_nvidia_smi(arguments: list[str], *, timeout: float = 10.0) -> str | None:
    if _NVIDIA_SMI is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603
            [_NVIDIA_SMI, *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _gpu_name() -> str | None:
    output = _run_nvidia_smi(["--query-gpu=name", "--format=csv,noheader"])
    if not output:
        return None
    for line in output.splitlines():
        name = line.strip()
        if name:
            return name
    return None


def _gpu_compute_apps() -> list[dict[str, Any]] | None:
    output = _run_nvidia_smi(
        [
            "--query-compute-apps=pid,process_name,used_memory",
            "--format=csv,noheader,nounits",
        ]
    )
    if output is None:
        return None
    apps: list[dict[str, Any]] = []
    own_pid = os.getpid()
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = [part.strip() for part in stripped.split(",")]
        if len(parts) < 3:
            continue
        try:
            pid: int | None = int(parts[0])
        except ValueError:
            pid = None
        apps.append(
            {
                "pid": pid,
                "name": _basename(parts[1]) or "",
                "used_mb": _parse_number(parts[2]),
                "is_self": pid == own_pid,
            }
        )
    return apps


class _ResourceSampler:
    """Sample CPU, load, and GPU metrics in a background thread."""

    def __init__(self, interval_ms: int, notes: list[str]) -> None:
        self._interval_ms = max(1, interval_ms)
        self._interval_s = self._interval_ms / 1000.0
        self._notes = notes
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._nvidia: subprocess.Popen[str] | None = None
        self._cpu_busy: list[float] = []
        self._process_cpu: list[float] = []
        self._gpu_mem: list[int] = []
        self._gpu_util: list[int] = []
        self._gpu_buffer = b""
        self._load_before: list[float] | None = None
        self._load_after: list[float] | None = None
        self._proc_start: float | None = None
        self._proc_end: float | None = None
        self._apps_before: list[dict[str, Any]] | None = None
        self._apps_after: list[dict[str, Any]] | None = None
        self._gpu_name: str | None = None
        self._gpu_available = False

    def start(self) -> None:
        self._load_before = _read_loadavg()
        self._proc_start = _proc_cpu_seconds()
        self._apps_before = _gpu_compute_apps()
        self._gpu_name = _gpu_name()
        self._gpu_available = self._apps_before is not None or self._gpu_name is not None
        if not self._gpu_available:
            self._notes.append(
                "nvidia-smi unavailable; GPU memory and utilization are reported as null"
            )
        if _NVIDIA_SMI is not None:
            try:
                self._nvidia = subprocess.Popen(  # noqa: S603
                    [
                        _NVIDIA_SMI,
                        "--query-gpu=memory.used,utilization.gpu",
                        "--format=csv,noheader,nounits",
                        f"--loop-ms={self._interval_ms}",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            except OSError:
                self._nvidia = None
                if self._gpu_available:
                    self._notes.append(
                        "nvidia-smi could not start; GPU memory and utilization "
                        "are reported as null"
                    )
                self._gpu_available = False
        if self._nvidia is not None and self._nvidia.stdout is not None:
            os.set_blocking(self._nvidia.stdout.fileno(), False)
        self._thread = threading.Thread(
            target=self._thread_main,
            name="bench-latency-resources",
            daemon=True,
        )
        self._thread.start()

    def _parse_gpu_line(self, line: str) -> None:
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            return
        memory = _parse_number(parts[0])
        utilization = _parse_number(parts[1])
        if memory is not None:
            self._gpu_mem.append(memory)
        if utilization is not None:
            self._gpu_util.append(utilization)

    def _flush_gpu_buffer(self) -> None:
        if not self._gpu_buffer:
            return
        line = self._gpu_buffer.decode("utf-8", "replace").strip()
        self._gpu_buffer = b""
        if line:
            self._parse_gpu_line(line)

    def _read_gpu_until(self, deadline: float) -> None:
        process = self._nvidia
        if process is None or process.stdout is None:
            self._stop.wait(min(0.05, max(0.0, deadline - time.monotonic())))
            return
        descriptor = process.stdout.fileno()
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select([descriptor], [], [], min(remaining, 0.05))
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                chunk = os.read(descriptor, 65536)
            except BlockingIOError:
                continue
            except OSError:
                break
            if not chunk:
                break
            self._gpu_buffer += chunk
            while b"\n" in self._gpu_buffer:
                line_bytes, self._gpu_buffer = self._gpu_buffer.split(b"\n", 1)
                line = line_bytes.decode("utf-8", "replace").strip()
                if line:
                    self._parse_gpu_line(line)
            if process.poll() is not None:
                break
        self._flush_gpu_buffer()

    def _terminate_nvidia(self) -> None:
        process = self._nvidia
        if process is None:
            return
        self._flush_gpu_buffer()
        if process.poll() is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    process.kill()
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=2.0)
        if process.stdout is not None:
            with suppress(OSError):
                process.stdout.close()
        self._nvidia = None

    def _thread_main(self) -> None:
        last_stat = _read_proc_stat()
        deadline = time.monotonic() + self._interval_s
        while not self._stop.is_set():
            self._read_gpu_until(deadline)
            if time.monotonic() < deadline:
                continue
            current_stat = _read_proc_stat()
            if last_stat is not None and current_stat is not None:
                busy_delta = current_stat[0] - last_stat[0]
                total_delta = current_stat[1] - last_stat[1]
                if total_delta > 0:
                    self._cpu_busy.append(max(0.0, min(100.0, busy_delta / total_delta * 100.0)))
            last_stat = current_stat
            process_cpu = _proc_cpu_seconds()
            if process_cpu is not None:
                self._process_cpu.append(process_cpu)
            deadline += self._interval_s
        self._terminate_nvidia()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_s + 2.0)
        self._terminate_nvidia()
        self._load_after = _read_loadavg()
        self._proc_end = _proc_cpu_seconds()
        self._apps_after = _gpu_compute_apps()

    def _contention(self) -> dict[str, Any]:
        own_end = None
        if self._apps_after is not None:
            own_end = next((app for app in self._apps_after if app["is_self"]), None)
        own_memory = 0
        if own_end is not None and own_end.get("used_mb") is not None:
            own_memory = int(own_end["used_mb"])
        used_gpu = own_memory > 0
        if used_gpu:
            note = (
                f"the measured pipeline held {own_memory} MiB of GPU memory at the end of the run"
            )
        else:
            note = (
                "the measured pipeline held 0 MiB of GPU memory: STT and TTS run on "
                "CPU onnxruntime, so this is expected"
            )
        return {
            "available": self._gpu_available,
            "start": self._apps_before or [],
            "end": self._apps_after or [],
            "own_pid": os.getpid(),
            "pipeline_gpu_memory_mb": own_memory,
            "pipeline_used_gpu": used_gpu,
            "note": note,
        }

    def summary(self) -> dict[str, Any]:
        if self._gpu_available and not self._gpu_mem:
            self._notes.append(
                "nvidia-smi produced no GPU memory samples; GPU memory and "
                "utilization are reported as null"
            )
        process_cpu_s: float | None = None
        if self._proc_start is not None and self._proc_end is not None:
            process_cpu_s = max(0.0, self._proc_end - self._proc_start)
        return {
            "sample_interval_ms": self._interval_ms,
            "cpu_count": os.cpu_count(),
            "loadavg_before": self._load_before,
            "loadavg_after": self._load_after,
            "process_cpu_s": process_cpu_s,
            "process_cpu_total_s": self._proc_end,
            "system_cpu_busy_pct": {
                "p50": _percentile(self._cpu_busy, 50.0),
                "max": max(self._cpu_busy) if self._cpu_busy else None,
            },
            "gpu_mem_used_mb": {
                "min": min(self._gpu_mem) if self._gpu_mem else None,
                "p50": _percentile([float(value) for value in self._gpu_mem], 50.0),
                "max": max(self._gpu_mem) if self._gpu_mem else None,
            },
            "gpu_util_pct": {
                "p50": _percentile([float(value) for value in self._gpu_util], 50.0),
                "max": max(self._gpu_util) if self._gpu_util else None,
            },
            "gpu_name": self._gpu_name,
            "gpu_available": self._gpu_available,
            "samples": {
                "cpu_intervals": len(self._cpu_busy),
                "process_cpu_intervals": len(self._process_cpu),
                "gpu_intervals": len(self._gpu_mem),
            },
            "gpu_contention": self._contention(),
        }


# ---------------------------------------------------------------------------
# Input preparation
# ---------------------------------------------------------------------------


def _load_cases(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise SpeechError(f"could not read cases file {path.name}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SpeechError(f"cases file {path.name} is not valid JSON: {exc}") from exc

    if isinstance(payload, dict):
        items = payload.get("utterances", [])
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    if not isinstance(items, list) or not items:
        raise SpeechError(f"no utterances found in cases file {path.name}")

    cases: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            cases.append({"id": len(cases) + 1, "text": item, "expect": ""})
        elif isinstance(item, dict) and str(item.get("text", "")).strip():
            cases.append(
                {
                    "id": item.get("id"),
                    "text": str(item["text"]),
                    "expect": str(item.get("expect", "")),
                }
            )
    if not cases:
        raise SpeechError(f"no usable utterances found in cases file {path.name}")
    return cases


def _require_model(
    value: Path | None,
    *,
    flag: str,
    env_var: str,
    expected: str,
) -> Path:
    if value is None:
        raise SpeechModelUnavailable(
            f"{flag} is not set: pass {flag} or set {env_var} to {expected}"
        )
    path = Path(value).expanduser()
    if not path.exists():
        raise SpeechModelUnavailable(
            f"{flag} does not exist: {path.name}; pass {flag} or set {env_var} to {expected}"
        )
    return path


def _load_wav_dir(directory: Path, case_count: int) -> list[tuple[bytes, int]]:
    paths = sorted(directory.glob("*.wav"))
    if not paths:
        raise SpeechError(f"--wav-dir {directory.name} contains no .wav files")
    if len(paths) < case_count:
        raise SpeechError(
            f"--wav-dir {directory.name} contains {len(paths)} wav files but "
            f"{case_count} utterances are scripted; provide one wav per utterance"
        )
    loaded: list[tuple[bytes, int]] = []
    for path in paths[:case_count]:
        try:
            with wave.open(str(path), "rb") as handle:
                channels = handle.getnchannels()
                width = handle.getsampwidth()
                rate = handle.getframerate()
                data = handle.readframes(handle.getnframes())
        except (OSError, wave.Error) as exc:
            raise SpeechError(f"could not read wav {path.name}: {exc}") from exc
        if channels != 1:
            raise SpeechError(
                f"wav {path.name} has {channels} channels; --wav-dir requires mono PCM16"
            )
        if width != 2:
            raise SpeechError(
                f"wav {path.name} uses {width * 8}-bit samples; --wav-dir requires mono PCM16"
            )
        loaded.append((data, rate))
    return loaded


async def _prepare_audio(
    case: dict[str, Any],
    engine: KokoroTtsEngine,
    settings: Settings,
    wavs: list[tuple[bytes, int]] | None,
    index: int,
) -> PcmChunk:
    if wavs is not None:
        data, rate = wavs[index]
    else:
        chunk = await engine.synthesize(
            case["text"],
            voice=settings.tts_voice,
            speed=settings.tts_speed,
        )
        data, rate = chunk.data, chunk.sample_rate
    trimmed, _trailing_ms, _speech_ms = _trim_trailing_silence(data, rate)
    if not trimmed:
        raise SpeechError(f"utterance {case.get('id')} is empty after trimming silence")
    return PcmChunk(data=trimmed, sample_rate=rate)


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------


async def _run_stt(
    adapter: SherpaStreamingStt,
    audio: PcmChunk,
    turn_commit_ms: int,
) -> dict[str, Any]:
    session = await adapter.start_session()
    result: dict[str, Any] = {
        "t_end_speech": None,
        "t_commit": None,
        "t_final": None,
        "transcript": "",
        "error": None,
    }
    try:
        frame_bytes = int(audio.sample_rate * _FRAME_MS / 1000.0) * 2
        for offset in range(0, len(audio.data), frame_bytes):
            frame = audio.data[offset : offset + frame_bytes]
            if not frame:
                continue
            await session.feed(PcmChunk(data=frame, sample_rate=audio.sample_rate))
        result["t_end_speech"] = time.perf_counter()

        # Digital silence in 20 ms frames.  This mirrors the shipped
        # EndOfTurnConfig.silence_commit_ms in relay/transport/turns.py; that
        # module is not imported because it is optional/uncommitted work.
        # The frames are paced at real time: the policy waits for real silence,
        # so feeding 600 ms of audio instantly would understate the perceived
        # response by almost the whole commit window.
        silence_frame_bytes = int(_STT_SAMPLE_RATE * _FRAME_MS / 1000.0) * 2
        remaining_ms = max(0, turn_commit_ms)
        while remaining_ms > 0:
            frame_ms = min(_FRAME_MS, remaining_ms)
            if frame_ms == _FRAME_MS:
                frame = b"\x00" * silence_frame_bytes
            else:
                frame = b"\x00" * (int(_STT_SAMPLE_RATE * frame_ms / 1000.0) * 2)
            await session.feed(PcmChunk(data=frame, sample_rate=_STT_SAMPLE_RATE))
            remaining_ms -= frame_ms
            await asyncio.sleep(frame_ms / 1000.0)
        result["t_commit"] = time.perf_counter()

        event = await session.flush()
        result["t_final"] = time.perf_counter()
        if event is not None:
            result["transcript"] = event.text
        if not result["transcript"]:
            result["error"] = "STT flush returned no non-empty transcript"
    finally:
        await session.close()
    return result


async def _run_tts_router(
    transcript: str,
    case: dict[str, Any],
    engine: KokoroTtsEngine,
    settings: Settings,
    router: VoiceBrainRouter,
    args: argparse.Namespace,
) -> dict[str, Any]:
    limits = BufferLimits(
        max_bytes=settings.speech_max_buffer_bytes,
        max_frames=settings.speech_max_buffer_frames,
        name="tts_audio_bench",
    )
    tts = StreamingTts(
        engine,
        voice=settings.tts_voice,
        speed=settings.tts_speed,
        opus_bit_rate=settings.opus_bit_rate,
        max_pending_sentences=settings.tts_max_pending_sentences,
        backpressure_timeout_s=settings.speech_backpressure_timeout_s,
        first_segment_max_chars=settings.tts_first_segment_max_chars,
        limits=limits,
    )
    state: dict[str, Any] = {
        "t_spoken": None,
        "t_first_audio": None,
        "t_last_audio": None,
        "audio_ms": 0.0,
        "packets": 0,
        "decision": None,
        "error": None,
        "flushed": False,
    }
    first_audio = asyncio.Event()
    drained = asyncio.Event()

    async def consume() -> None:
        try:
            async for chunk in tts.chunks():
                if not chunk.opus_packets:
                    continue
                now = time.perf_counter()
                if state["t_first_audio"] is None:
                    state["t_first_audio"] = now
                    first_audio.set()
                state["t_last_audio"] = now
                state["audio_ms"] += chunk.duration_ms
                state["packets"] += len(chunk.opus_packets)
        except Exception as exc:
            state["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            drained.set()

    async def produce() -> None:
        async for event in router.route(
            transcript,
            attachments_pending=(case.get("expect") == "attach_file"),
        ):
            kind = event.kind
            if kind == "done":
                state["decision"] = event.decision or state["decision"]
                break
            if kind == "tool_call":
                state["decision"] = event.decision or state["decision"]
            if kind in {"ack", "text_delta"}:
                if state["t_spoken"] is None:
                    state["t_spoken"] = time.perf_counter()
                if event.text:
                    await tts.push_text(event.text)
        await tts.flush()
        state["flushed"] = True

    consumer_task = asyncio.create_task(consume())
    producer_task = asyncio.create_task(produce())
    try:
        try:
            await asyncio.wait_for(first_audio.wait(), timeout=args.first_audio_timeout_s)
        except TimeoutError:
            state["error"] = f"no first audio within {args.first_audio_timeout_s:.1f}s"
        await producer_task
        try:
            await asyncio.wait_for(drained.wait(), timeout=args.drain_timeout_s)
        except TimeoutError:
            drain_error = f"audio drain exceeded {args.drain_timeout_s:.1f}s"
            if state["error"] is None:
                state["error"] = drain_error
            else:
                state["error"] = f"{state['error']}; {drain_error}"
    finally:
        await tts.close()
        if not consumer_task.done():
            consumer_task.cancel()
            with suppress(asyncio.CancelledError):
                await consumer_task
        else:
            with suppress(asyncio.CancelledError):
                await consumer_task
    return state


def _row_metrics(
    case: dict[str, Any],
    repeat_index: int,
    stt_result: dict[str, Any],
    pipeline: dict[str, Any] | None,
    error: str | None,
) -> dict[str, Any]:
    t_end_speech = stt_result.get("t_end_speech")
    t_commit = stt_result.get("t_commit")
    t_final = stt_result.get("t_final")
    decision = pipeline.get("decision") if pipeline else None
    t_spoken = pipeline.get("t_spoken") if pipeline else None
    t_first_audio = pipeline.get("t_first_audio") if pipeline else None
    t_last_audio = pipeline.get("t_last_audio") if pipeline else None

    def delta_ms(later: float | None, earlier: float | None) -> float | None:
        if later is None or earlier is None:
            return None
        return (later - earlier) * 1000.0

    transcript = str(stt_result.get("transcript", ""))
    row_error = error
    if row_error is None and pipeline is not None:
        row_error = pipeline.get("error")
    return {
        "utterance_id": case.get("id"),
        "repeat": repeat_index + 1,
        "expected_tool": case.get("expect", ""),
        "text": case.get("text", ""),
        "transcript": transcript,
        "word_error_rate": _word_error_rate(str(case.get("text", "")), transcript),
        "turn_commit_ms": delta_ms(t_commit, t_end_speech),
        "stt_finalize_ms": delta_ms(t_final, t_commit),
        "router_first_spoken_ms": delta_ms(t_spoken, t_final),
        "tts_first_audio_ms": delta_ms(t_first_audio, t_spoken),
        "e2e_compute_ms": delta_ms(t_first_audio, t_commit),
        "perceived_ms": delta_ms(t_first_audio, t_end_speech),
        "reply_complete_ms": delta_ms(t_last_audio, t_end_speech),
        "audio_ms": pipeline.get("audio_ms") if pipeline else None,
        "packets": pipeline.get("packets") if pipeline else None,
        "router_ttft_ms": getattr(decision, "ttft_ms", None),
        "router_total_ms": getattr(decision, "total_ms", None),
        "tool": getattr(decision, "tool", None),
        "source": getattr(decision, "source", None),
        "error": _scrub(row_error) if row_error else None,
    }


def _stage_summaries(rows: list[dict[str, Any]]) -> dict[str, Any]:
    stages: dict[str, Any] = {}
    for metric in _NUMERIC_METRICS:
        values = [float(row[metric]) for row in rows if isinstance(row.get(metric), (int, float))]
        stages[metric] = _summary(values)
    return stages


def _per_tool_summaries(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        tool = str(row.get("tool") or "unknown")
        groups.setdefault(tool, []).append(row)
    result: dict[str, Any] = {}
    for tool in sorted(groups):
        group = groups[tool]
        entry: dict[str, Any] = {"count": len(group)}
        for metric in _TOOL_METRICS:
            values = [
                float(row[metric]) for row in group if isinstance(row.get(metric), (int, float))
            ]
            entry[metric] = _summary(values)
        result[tool] = entry
    return result


def _git_commit() -> str | None:
    if _GIT is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603
            [_GIT, "rev-parse", "--short", "HEAD"],
            cwd=_REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    commit = completed.stdout.strip()
    return commit or None


async def _run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    notes: list[str] = [
        "STT and TTS run on CPU onnxruntime; the local llama.cpp endpoint is "
        "used only for routing.",
    ]
    settings = Settings(_env_file=None)
    cases = _load_cases(args.cases)
    fingerprint = _utterance_fingerprint(cases)

    stt_model_dir = _require_model(
        args.stt_model_dir,
        flag="--stt-model-dir",
        env_var="SPEECH_MODEL_DIR",
        expected=(
            "a sherpa-onnx model directory containing tokens.txt, encoder-*.onnx, "
            "decoder-*.onnx and joiner-*.onnx"
        ),
    )
    tts_model = _require_model(
        args.tts_model,
        flag="--tts-model",
        env_var="TTS_MODEL_PATH",
        expected="a Kokoro ONNX model file (for example kokoro-v1.0.int8.onnx)",
    )
    tts_voices = _require_model(
        args.tts_voices,
        flag="--tts-voices",
        env_var="TTS_VOICES_PATH",
        expected="the matching Kokoro voices .bin file",
    )

    router_config = RouterConfig(
        base_url=args.url,
        model=args.model,
        temperature=settings.voice_brain_temperature,
        max_tokens=settings.voice_brain_max_tokens,
        tool_choice=settings.voice_brain_tool_choice,
        deferred_tools=settings.voice_brain_deferred_tools,
        first_token_timeout_s=settings.voice_brain_first_token_timeout_s,
        request_timeout_s=settings.voice_brain_request_timeout_s,
        enable_thinking=bool(getattr(settings, "voice_brain_enable_thinking", False)),
        max_utterance_chars=settings.voice_brain_max_utterance_chars,
    )
    router = VoiceBrainRouter(router_config)

    cold_start_started = time.perf_counter()
    engine = KokoroTtsEngine(tts_model, tts_voices)
    cold_start_ms = (time.perf_counter() - cold_start_started) * 1000.0
    warmup_tts = StreamingTts(
        engine,
        voice=settings.tts_voice,
        speed=settings.tts_speed,
        opus_bit_rate=settings.opus_bit_rate,
        max_pending_sentences=settings.tts_max_pending_sentences,
        backpressure_timeout_s=settings.speech_backpressure_timeout_s,
        first_segment_max_chars=settings.tts_first_segment_max_chars,
        limits=BufferLimits(
            max_bytes=settings.speech_max_buffer_bytes,
            max_frames=settings.speech_max_buffer_frames,
            name="tts_audio_bench_warmup",
        ),
    )
    warmup_ms = await warmup_tts.warmup()
    await warmup_tts.close()

    stt = SherpaStreamingStt(
        stt_model_dir,
        num_threads=settings.stt_num_threads,
        partial_interval_ms=settings.stt_partial_interval_ms,
        flush_padding_ms=settings.stt_flush_padding_ms,
    )

    wavs = _load_wav_dir(args.wav_dir, len(cases)) if args.wav_dir is not None else None
    if wavs is not None:
        notes.append(f"used {len(wavs)} pre-rendered mono PCM16 wavs from {args.wav_dir.name}")

    started_at = datetime.now(UTC).isoformat()
    sampler = _ResourceSampler(args.sample_interval_ms, notes)
    sampler.start()
    rows: list[dict[str, Any]] = []
    try:
        for repeat_index in range(args.repeat):
            for case_index, case in enumerate(cases):
                print(
                    f"bench_latency: repeat {repeat_index + 1}/{args.repeat} "
                    f"utterance {case_index + 1}/{len(cases)}",
                    file=sys.stderr,
                )
                stt_result: dict[str, Any] = {
                    "t_end_speech": None,
                    "t_commit": None,
                    "t_final": None,
                    "transcript": "",
                    "error": None,
                }
                pipeline: dict[str, Any] | None = None
                error: str | None = None
                try:
                    audio = await _prepare_audio(
                        case,
                        engine,
                        settings,
                        wavs,
                        case_index,
                    )
                    stt_result = await _run_stt(stt, audio, args.turn_commit_ms)
                    if stt_result["error"] is not None:
                        error = stt_result["error"]
                    else:
                        pipeline = await _run_tts_router(
                            stt_result["transcript"],
                            case,
                            engine,
                            settings,
                            router,
                            args,
                        )
                except (RouterUnavailable, RouterTimeout):
                    raise
                except SpeechModelUnavailable:
                    raise
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                rows.append(_row_metrics(case, repeat_index, stt_result, pipeline, error))
    finally:
        sampler.stop()
        await router.aclose()

    resources = sampler.summary()
    stages = _stage_summaries(rows)
    per_tool = _per_tool_summaries(rows)
    acceptance = _acceptance(
        stages.get("perceived_ms", {}).get("p50"),
        args.max_perceived_p50_ms,
    )
    error_rows = sum(1 for row in rows if row.get("error"))
    if error_rows:
        notes.append(
            f"{error_rows} of {len(rows)} rows recorded an error; latency summaries "
            "exclude missing values"
        )
    if not _is_loopback_url(args.url):
        notes.append("the router endpoint is not loopback; latency includes the network hop")

    return {
        "schema_version": _SCHEMA_VERSION,
        "config": {
            "utterance_set_sha256": fingerprint,
            "cases": cases,
            "repeat": args.repeat,
            "turn_commit_ms": args.turn_commit_ms,
            "router": {
                "model": args.model,
                "base_url": args.url,
                "temperature": settings.voice_brain_temperature,
                "max_tokens": settings.voice_brain_max_tokens,
                "tool_choice": settings.voice_brain_tool_choice,
                "deferred": settings.voice_brain_deferred_tools,
            },
            "stt": {
                "engine": "sherpa-onnx",
                "model": stt_model_dir.name,
                "num_threads": settings.stt_num_threads,
                "partial_interval_ms": settings.stt_partial_interval_ms,
                "flush_padding_ms": settings.stt_flush_padding_ms,
            },
            "tts": {
                "engine": "kokoro",
                "model": tts_model.name,
                "voice": settings.tts_voice,
                "speed": settings.tts_speed,
                "opus_bit_rate": settings.opus_bit_rate,
                "first_segment_max_chars": settings.tts_first_segment_max_chars,
            },
        },
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "git_commit": _git_commit(),
            "started_at_utc": started_at,
            "gpu_name": resources.get("gpu_name"),
        },
        "warmup": {
            "cold_start_ms": cold_start_ms,
            "warmup_ms": warmup_ms,
        },
        "stages": stages,
        "per_tool": per_tool,
        "acceptance": acceptance,
        "resources": resources,
        "rows": rows,
        "notes": [_scrub(note) for note in notes],
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _print_human_report(report: dict[str, Any]) -> None:
    stages = report.get("stages", {})
    print("bench_latency: end-to-end latency by stage (ms)")
    print(f"{'stage':<26} {'p50':>10} {'p95':>10} {'max':>10}")
    for metric in _TABLE_METRICS:
        summary = stages.get(metric, {})
        print(
            f"{metric:<26} {_fmt_ms(summary.get('p50')):>10} "
            f"{_fmt_ms(summary.get('p95')):>10} {_fmt_ms(summary.get('max')):>10}"
        )

    acceptance = report.get("acceptance", {})
    status = "MET" if acceptance.get("met") else "NOT MET"
    print(
        "acceptance: perceived p50="
        f"{_fmt_ms(acceptance.get('perceived_p50_ms'))} ms "
        f"threshold={_fmt_ms(acceptance.get('threshold_ms'))} ms -> {status}"
    )

    resources = report.get("resources", {})
    cpu = resources.get("system_cpu_busy_pct", {})
    gpu_mem = resources.get("gpu_mem_used_mb", {})
    gpu_util = resources.get("gpu_util_pct", {})
    print(
        "resources: cpu_count="
        f"{resources.get('cpu_count')} system_cpu_busy_pct p50="
        f"{_fmt_ms(cpu.get('p50'))} max={_fmt_ms(cpu.get('max'))} "
        f"process_cpu_s={_fmt_ms(resources.get('process_cpu_s'))}"
    )
    print(
        "resources: gpu_mem_used_mb min="
        f"{_fmt_ms(gpu_mem.get('min'))} p50={_fmt_ms(gpu_mem.get('p50'))} "
        f"max={_fmt_ms(gpu_mem.get('max'))} gpu_util_pct p50="
        f"{_fmt_ms(gpu_util.get('p50'))} max={_fmt_ms(gpu_util.get('max'))}"
    )
    print(
        "resources: loadavg_before="
        f"{resources.get('loadavg_before')} loadavg_after="
        f"{resources.get('loadavg_after')} gpu_name={resources.get('gpu_name')}"
    )

    contention = resources.get("gpu_contention", {})
    for phase in ("start", "end"):
        apps = contention.get(phase) or []
        if apps:
            rendered = ", ".join(
                f"{app.get('name')}(pid={app.get('pid')}, "
                f"{app.get('used_mb')} MiB{', self' if app.get('is_self') else ''})"
                for app in apps
            )
        else:
            rendered = "none"
        print(f"gpu contention ({phase}): {rendered}")
    print(
        "gpu contention: measured pipeline gpu_mem="
        f"{contention.get('pipeline_gpu_memory_mb')} MiB "
        f"used_gpu={contention.get('pipeline_used_gpu')} "
        f"({contention.get('note')})"
    )

    warmup = report.get("warmup", {})
    print(
        "warmup: cold_start_ms="
        f"{_fmt_ms(warmup.get('cold_start_ms'))} "
        f"warmup_ms={_fmt_ms(warmup.get('warmup_ms'))}"
    )

    per_tool = report.get("per_tool", {})
    for tool in sorted(per_tool):
        entry = per_tool[tool]
        perceived = entry.get("perceived_ms", {})
        print(
            f"per_tool: {tool} count={entry.get('count')} "
            f"perceived_p50={_fmt_ms(perceived.get('p50'))} "
            f"perceived_p95={_fmt_ms(perceived.get('p95'))}"
        )

    notes = report.get("notes", [])
    for note in notes:
        print(f"note: {note}")


def _print_comparison(
    baseline_path: Path,
    report: dict[str, Any],
    regression_pct: float,
) -> int:
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except OSError as exc:
        print(
            f"bench_latency: error: could not read baseline {baseline_path}: {exc}", file=sys.stderr
        )
        return 1
    except json.JSONDecodeError as exc:
        print(
            f"bench_latency: error: baseline {baseline_path} is not valid JSON: {exc}",
            file=sys.stderr,
        )
        return 1
    if not isinstance(baseline, dict) or baseline.get("schema_version") != _SCHEMA_VERSION:
        found = baseline.get("schema_version") if isinstance(baseline, dict) else None
        print(
            "bench_latency: error: baseline schema_version mismatch: "
            f"found {found!r}, expected {_SCHEMA_VERSION}",
            file=sys.stderr,
        )
        return 1

    comparability = _comparability(
        baseline.get("config", {}),
        report.get("config", {}),
    )
    print(f"compare: comparable={comparability['comparable']}")
    for mismatch in comparability["mismatches"]:
        print(f"compare: mismatch {mismatch}")

    comparison = _compare_summaries(
        baseline.get("stages", {}),
        report.get("stages", {}),
        regression_pct,
    )
    print(
        f"{'stage':<26} {'p50 base':>10} {'p50 now':>10} {'p50 delta':>10} "
        f"{'p50 pct':>9} {'p95 base':>10} {'p95 now':>10} {'p95 delta':>10} "
        f"{'p95 pct':>9} regression"
    )
    for metric in sorted(comparison):
        entry = comparison[metric]
        p50 = entry.get("p50", {})
        p95 = entry.get("p95", {})
        regression = "yes" if p50.get("regression") or p95.get("regression") else "no"
        print(
            f"{metric:<26} {_fmt_ms(p50.get('baseline')):>10} "
            f"{_fmt_ms(p50.get('current')):>10} {_fmt_delta(p50.get('delta_ms')):>10} "
            f"{_fmt_pct(p50.get('delta_pct')):>9} "
            f"{_fmt_ms(p95.get('baseline')):>10} {_fmt_ms(p95.get('current')):>10} "
            f"{_fmt_delta(p95.get('delta_ms')):>10} {_fmt_pct(p95.get('delta_pct')):>9} "
            f"{regression}"
        )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _is_loopback_url(url: str) -> bool:
    host = urlsplit(url).hostname
    if host is None:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = Settings(_env_file=None)
    default_stt = os.environ.get("SPEECH_MODEL_DIR") or (
        str(settings.stt_model_dir) if settings.stt_model_dir is not None else None
    )
    default_tts_model = os.environ.get("TTS_MODEL_PATH") or (
        str(settings.tts_model_path) if settings.tts_model_path is not None else None
    )
    default_tts_voices = os.environ.get("TTS_VOICES_PATH") or (
        str(settings.tts_voices_path) if settings.tts_voices_path is not None else None
    )
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("VOICE_BRAIN_BASE_URL") or settings.voice_brain_base_url,
        help=(
            "voice-brain router base URL (default: VOICE_BRAIN_BASE_URL or the "
            "configured Settings value, normally http://127.0.0.1:8080); a "
            "non-loopback host prints a warning"
        ),
    )
    parser.add_argument(
        "--model",
        default=settings.voice_brain_model,
        help="router model name (default: the configured Settings voice_brain_model)",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=_REPO_ROOT / "scripts" / "voice_bench_utterances.json",
        help=(
            "JSON file with the scripted utterances (default: "
            "scripts/voice_bench_utterances.json in this repository)"
        ),
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="run the scripted cases this many times in file order (default: 1)",
    )
    parser.add_argument(
        "--turn-commit-ms",
        type=int,
        default=600,
        help=(
            "digital silence fed to STT after the speech ends, before flush "
            "(default: 600; mirrors EndOfTurnConfig.silence_commit_ms)"
        ),
    )
    parser.add_argument(
        "--stt-model-dir",
        type=Path,
        default=Path(default_stt) if default_stt else None,
        help=(
            "sherpa-onnx STT model directory (default: SPEECH_MODEL_DIR or the "
            "configured Settings stt_model_dir); must contain tokens.txt and "
            "encoder/decoder/joiner *.onnx files"
        ),
    )
    parser.add_argument(
        "--tts-model",
        type=Path,
        default=Path(default_tts_model) if default_tts_model else None,
        help="Kokoro ONNX model file (default: TTS_MODEL_PATH)",
    )
    parser.add_argument(
        "--tts-voices",
        type=Path,
        default=Path(default_tts_voices) if default_tts_voices else None,
        help="Kokoro voices .bin file (default: TTS_VOICES_PATH)",
    )
    parser.add_argument(
        "--wav-dir",
        type=Path,
        help=(
            "directory of sorted mono PCM16 .wav files, one per scripted case; "
            "replaces Kokoro rendering of the user utterances (reply TTS still "
            "uses Kokoro)"
        ),
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        help="write the full schema_version 1 JSON report to this path",
    )
    parser.add_argument(
        "--compare",
        type=Path,
        help=(
            "compare against a previous schema_version 1 report; prints absolute "
            "and percent deltas and regression flags"
        ),
    )
    parser.add_argument(
        "--regression-pct",
        type=float,
        default=10.0,
        help="flag stage regressions above this percent in --compare output (default: 10)",
    )
    parser.add_argument(
        "--max-perceived-p50-ms",
        type=float,
        default=1200.0,
        help="acceptance threshold for perceived p50 latency in ms (default: 1200)",
    )
    parser.add_argument(
        "--sample-interval-ms",
        type=int,
        default=250,
        help="CPU/GPU resource sampling interval in ms (default: 250)",
    )
    parser.add_argument(
        "--first-audio-timeout-s",
        type=float,
        default=10.0,
        help="maximum wait for the first Opus audio packet in seconds (default: 10)",
    )
    parser.add_argument(
        "--drain-timeout-s",
        type=float,
        default=20.0,
        help="maximum wait to drain the reply audio stream in seconds (default: 20)",
    )
    args = parser.parse_args(argv)

    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.turn_commit_ms < 0:
        parser.error("--turn-commit-ms must be zero or greater")
    if args.sample_interval_ms < 1:
        parser.error("--sample-interval-ms must be at least 1")
    if args.first_audio_timeout_s <= 0:
        parser.error("--first-audio-timeout-s must be greater than zero")
    if args.drain_timeout_s <= 0:
        parser.error("--drain-timeout-s must be greater than zero")
    if args.regression_pct < 0:
        parser.error("--regression-pct must be zero or greater")
    if args.max_perceived_p50_ms <= 0:
        parser.error("--max-perceived-p50-ms must be greater than zero")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
        if not _is_loopback_url(args.url):
            print(
                f"bench_latency: warning: router endpoint {args.url} is not loopback; "
                "measured latency includes the network hop",
                file=sys.stderr,
            )
        report = asyncio.run(_run_benchmark(args))
    except SpeechModelUnavailable as exc:
        print(f"bench_latency: error: {exc}", file=sys.stderr)
        return 1
    except RouterError as exc:
        print(f"bench_latency: error: {exc}", file=sys.stderr)
        return 1
    except SpeechError as exc:
        print(f"bench_latency: error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"bench_latency: error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    _print_human_report(report)

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"bench_latency: wrote {args.json_out}")

    if args.compare is not None:
        comparison_status = _print_comparison(args.compare, report, args.regression_pct)
        if comparison_status != 0:
            return comparison_status

    if report["acceptance"]["met"]:
        return 0
    print(
        "bench_latency: acceptance not met: perceived p50 "
        f"{_fmt_ms(report['acceptance']['perceived_p50_ms'])} ms is not below "
        f"{_fmt_ms(report['acceptance']['threshold_ms'])} ms",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
