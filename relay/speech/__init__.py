"""Local-only speech input and output adapters for the Hermes Talk relay.

The package is deliberately import-safe: heavy dependencies (``numpy``, ``av``,
``sherpa_onnx``, ``kokoro_onnx``, ``faster_whisper``) are imported lazily inside
the functions that need them.  Importing :mod:`relay.speech` therefore works on
a machine that only installed the relay's development extras.
"""

from __future__ import annotations

from relay.speech.errors import (
    SpeechBackpressureExceeded,
    SpeechError,
    SpeechModelUnavailable,
    SpeechStateError,
)

__all__ = [
    "SpeechBackpressureExceeded",
    "SpeechError",
    "SpeechModelUnavailable",
    "SpeechStateError",
]
