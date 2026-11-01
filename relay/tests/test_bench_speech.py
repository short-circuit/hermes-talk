"""Offline tests for the benchmark script's pure helpers.

The benchmark is a script rather than a package, so it is loaded by path with
``importlib``.  These tests do not touch any models and stay well under a
second.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bench_speech.py"


def _load_bench_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_speech_under_test", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bench() -> ModuleType:
    return _load_bench_module()


def test_percentile_interpolates_and_handles_empty(bench: ModuleType) -> None:
    assert bench._percentile([0.0, 10.0], 50.0) == pytest.approx(5.0)
    assert bench._percentile([1.0, 2.0, 3.0], 95.0) == pytest.approx(2.9)
    assert bench._percentile([], 50.0) is None


def test_summary_reports_empty_and_populated_values(bench: ModuleType) -> None:
    empty = bench._summary([])
    assert empty["count"] == 0
    assert empty["p50"] is None
    assert empty["p95"] is None
    assert empty["max"] is None

    summary = bench._summary([1.0, 2.0, 3.0])
    assert summary["count"] == 3
    assert summary["p50"] == pytest.approx(2.0)
    assert summary["max"] == 3.0


def test_classify_final_path_prefers_endpoint_then_flush(bench: ModuleType) -> None:
    assert bench._classify_final_path(endpoint=True, flush=True) == "endpoint"
    assert bench._classify_final_path(endpoint=False, flush=True) == "flush"
    assert bench._classify_final_path(endpoint=False, flush=False) == "none"


def test_path_counts_includes_zero_count_entries(bench: ModuleType) -> None:
    counts = bench._path_counts(["endpoint", "flush", "flush"])
    assert counts == {"endpoint": 1, "flush": 2, "none": 0}


def test_word_error_rate_counts_substitutions(bench: ModuleType) -> None:
    assert bench._word_error_rate("hello world", "hello world") == 0.0
    assert bench._word_error_rate("hello world", "hello there") == pytest.approx(0.5)
    assert bench._word_error_rate("", "") == 0.0
    assert bench._word_error_rate("", "extra") == 1.0
