"""Hermes run adapter: HTTP client, SSE decoder and stop control."""

from __future__ import annotations

from relay.hermes.client import (
    HermesAuthError,
    HermesClient,
    HermesConfigError,
    HermesConflictError,
    HermesError,
    HermesNotFoundError,
    HermesProtocolError,
    HermesRunGoneError,
    HermesStreamError,
    HermesTimeoutError,
    HermesUnavailableError,
    RunAdmission,
    RunStatus,
    StopResult,
)
from relay.hermes.events import (
    TERMINAL_KINDS,
    EventGate,
    EventKind,
    HermesEventError,
    RunEvent,
    SseDecoder,
    SseFrame,
    decode_event,
)

__all__ = [
    "TERMINAL_KINDS",
    "EventGate",
    "EventKind",
    "HermesAuthError",
    "HermesClient",
    "HermesConfigError",
    "HermesConflictError",
    "HermesError",
    "HermesEventError",
    "HermesNotFoundError",
    "HermesProtocolError",
    "HermesRunGoneError",
    "HermesStreamError",
    "HermesTimeoutError",
    "HermesUnavailableError",
    "RunAdmission",
    "RunEvent",
    "RunStatus",
    "SseDecoder",
    "SseFrame",
    "StopResult",
    "decode_event",
]
