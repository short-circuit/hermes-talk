"""Error types shared by the local speech adapters.

Every adapter failure is reported through one of these exceptions so callers can
distinguish a missing model (fix the environment) from a programming error (fix
the call order) or an overloaded pipeline (slow down or drop audio).
"""

from __future__ import annotations

from typing import Any


class SpeechError(RuntimeError):
    """Base class for speech adapter failures."""


class SpeechModelUnavailable(SpeechError):
    """A package, model directory, or model file is missing.

    Messages must name the engine, what was expected, and the environment
    variable that fixes the problem.
    """


class SpeechBackpressureExceeded(SpeechError):
    """A bounded queue or buffer could not accept audio within its limit."""

    def __init__(
        self,
        limit_name: str,
        limit_value: Any,
        observed_value: Any,
        *,
        detail: str | None = None,
    ) -> None:
        self.limit_name = limit_name
        self.limit_value = limit_value
        self.observed_value = observed_value
        message = detail or (
            f"{limit_name} limit exceeded: limit={limit_value}, observed={observed_value}"
        )
        super().__init__(message)


class SpeechStateError(SpeechError):
    """An adapter method was called in the wrong order."""

    def __init__(self, detail: str = "speech adapter is in an invalid state") -> None:
        self.detail = detail
        super().__init__(detail)
