"""Local-only turn detection, barge-in, and interruption for the relay.

Everything here is import-safe: heavy audio dependencies are imported lazily,
and no network call is made unless an injected ``RunStopper`` is asked to stop
a run.
"""

from __future__ import annotations

from relay.transport.turns import (
    DEFAULT_FLUSH_BUDGET_MS,
    INTERRUPTION_MARKER_TEMPLATE,
    MAX_SPOKEN_TAIL_CHARS,
    BargeInResult,
    EndOfTurnConfig,
    EndOfTurnDetector,
    EnergyVad,
    HttpRunStopper,
    Interruption,
    MicOutcome,
    PlaybackSink,
    RunStopper,
    RunStopResult,
    TtsPlayback,
    TurnConfigError,
    TurnController,
    TurnDecision,
    TurnError,
    TurnEvent,
    TurnLedger,
    TurnRecord,
    TurnState,
    TurnStateError,
    VadConfig,
    VadEngine,
    VadFrame,
    VadState,
)

__all__ = [
    "DEFAULT_FLUSH_BUDGET_MS",
    "INTERRUPTION_MARKER_TEMPLATE",
    "MAX_SPOKEN_TAIL_CHARS",
    "BargeInResult",
    "EndOfTurnConfig",
    "EndOfTurnDetector",
    "EnergyVad",
    "HttpRunStopper",
    "Interruption",
    "MicOutcome",
    "PlaybackSink",
    "RunStopResult",
    "RunStopper",
    "TtsPlayback",
    "TurnConfigError",
    "TurnController",
    "TurnDecision",
    "TurnError",
    "TurnEvent",
    "TurnLedger",
    "TurnRecord",
    "TurnState",
    "TurnStateError",
    "VadConfig",
    "VadEngine",
    "VadFrame",
    "VadState",
]
