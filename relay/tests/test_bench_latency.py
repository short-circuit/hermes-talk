"""Offline tests for the latency benchmark's pure helpers.

The benchmark is a standalone script rather than a package module, so it is
loaded by path with ``importlib`` (mirroring ``test_bench_speech.py``).  These
tests never load a model and finish in well under a second.
"""

from __future__ import annotations

import array
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().with_name("bench_latency.py")


def _load_bench_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_latency_under_test", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bench() -> ModuleType:
    return _load_bench_module()


def _pcm16(samples: list[int]) -> bytes:
    values = array.array("h", samples)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def test_percentile_interpolates_and_handles_empty(bench: ModuleType) -> None:
    assert bench._percentile([0.0, 10.0], 50.0) == pytest.approx(5.0)
    assert bench._percentile([1.0, 2.0, 3.0], 95.0) == pytest.approx(2.9)
    assert bench._percentile([7.0], 95.0) == pytest.approx(7.0)
    assert bench._percentile([], 50.0) is None


def test_summary_reports_empty_and_populated_values(bench: ModuleType) -> None:
    empty = bench._summary([])
    assert empty == {"count": 0, "p50": None, "p95": None, "max": None, "mean": None}

    summary = bench._summary([1.0, 2.0, 3.0])
    assert summary["count"] == 3
    assert summary["p50"] == pytest.approx(2.0)
    assert summary["p95"] == pytest.approx(2.9)
    assert summary["max"] == 3.0
    assert summary["mean"] == pytest.approx(2.0)


def test_acceptance_boundary_is_strictly_below_threshold(bench: ModuleType) -> None:
    met = bench._acceptance(1199.0, 1200.0)
    assert met["met"] is True
    assert met["perceived_p50_ms"] == 1199.0
    assert met["threshold_ms"] == 1200.0

    not_met = bench._acceptance(1200.0, 1200.0)
    assert not_met["met"] is False

    assert bench._acceptance(None, 1200.0)["met"] is False


def test_compare_summaries_produces_deltas_and_regression_flags(bench: ModuleType) -> None:
    baseline = {
        "perceived_ms": {"p50": 100.0, "p95": 200.0},
        "tts_first_audio_ms": {"p50": 50.0, "p95": 80.0},
    }
    current = {
        "perceived_ms": {"p50": 120.0, "p95": 180.0},
        "tts_first_audio_ms": {"p50": 40.0, "p95": 90.0},
        "new_metric_ms": {"p50": 10.0, "p95": 20.0},
    }

    comparison = bench._compare_summaries(baseline, current, 10.0)

    perceived = comparison["perceived_ms"]
    assert perceived["p50"]["delta_ms"] == pytest.approx(20.0)
    assert perceived["p50"]["delta_pct"] == pytest.approx(20.0)
    assert perceived["p50"]["regression"] is True
    assert perceived["p95"]["delta_ms"] == pytest.approx(-20.0)
    assert perceived["p95"]["delta_pct"] == pytest.approx(-10.0)
    assert perceived["p95"]["regression"] is False

    tts = comparison["tts_first_audio_ms"]
    assert tts["p50"]["regression"] is False
    assert tts["p95"]["delta_pct"] == pytest.approx(12.5)
    assert tts["p95"]["regression"] is True

    new_metric = comparison["new_metric_ms"]
    assert new_metric["p50"]["delta_ms"] is None
    assert new_metric["p50"]["delta_pct"] is None
    assert new_metric["p50"]["regression"] is False


def test_fingerprint_is_stable_and_changes_with_a_case(bench: ModuleType) -> None:
    cases = [
        {"id": 1, "text": "What time is it?", "expect": "answer_direct"},
        {"id": 2, "text": "Turn off the lights.", "expect": "delegate_to_hermes"},
    ]
    assert bench._utterance_fingerprint(cases) == bench._utterance_fingerprint(
        [dict(case) for case in cases]
    )

    changed = [dict(case) for case in cases]
    changed[0]["text"] = "What is the time?"
    assert bench._utterance_fingerprint(changed) != bench._utterance_fingerprint(cases)

    changed_expectation = [dict(case) for case in cases]
    changed_expectation[1]["expect"] = "answer_direct"
    assert bench._utterance_fingerprint(changed_expectation) != bench._utterance_fingerprint(cases)


def test_trim_trailing_silence_requires_a_twenty_ms_run(bench: ModuleType) -> None:
    sample_rate = 16000
    speech = [1000] * 100

    short_silence = speech + [0] * 160  # 10 ms
    trimmed, trailing_ms, speech_ms = bench._trim_trailing_silence(
        _pcm16(short_silence), sample_rate
    )
    assert trimmed == _pcm16(short_silence)
    assert trailing_ms == pytest.approx(0.0)
    assert speech_ms == pytest.approx(len(short_silence) / sample_rate * 1000.0)

    long_silence = speech + [0] * 480  # 30 ms
    trimmed, trailing_ms, speech_ms = bench._trim_trailing_silence(
        _pcm16(long_silence), sample_rate
    )
    assert trimmed == _pcm16(speech)
    assert trailing_ms == pytest.approx(30.0)
    assert speech_ms == pytest.approx(len(speech) / sample_rate * 1000.0)

    # Interior silence is preserved; only the trailing run is removed.
    interior = speech + [0] * 480 + speech + [0] * 480
    trimmed, trailing_ms, _speech_ms = bench._trim_trailing_silence(_pcm16(interior), sample_rate)
    assert trimmed == _pcm16(speech + [0] * 480 + speech)
    assert trailing_ms == pytest.approx(30.0)


def test_word_error_rate_counts_substitutions(bench: ModuleType) -> None:
    assert bench._word_error_rate("hello world", "hello world") == 0.0
    assert bench._word_error_rate("hello world", "hello there") == pytest.approx(0.5)
    assert bench._word_error_rate("", "") == 0.0
    assert bench._word_error_rate("", "extra") == 1.0
