package com.example.hermestalk.wakeword

/** Deterministic wake-word lifecycle states surfaced to the UI. */
sealed interface WakeWordState {
    /** No engine or capture is active. */
    data object Off : WakeWordState

    /** The engine is being created and capture is starting. */
    data object Starting : WakeWordState

    /** Capture is running and the keyword spotter is listening. */
    data object Listening : WakeWordState

    /** A keyword was detected; the gate waits for an explicit cooldown event. */
    data object Detected : WakeWordState

    /** Capture is stopping and resources are being released. */
    data object Stopping : WakeWordState

    /** The engine or its native library is unavailable. */
    data class Unavailable(val reason: String) : WakeWordState

    /** The microphone runtime permission was denied. */
    data object PermissionDenied : WakeWordState

    /** Capture failed after starting. */
    data class Failed(val reason: String) : WakeWordState
}

/** True while the engine or capture is running. */
val WakeWordState.isActive: Boolean
    get() = when (this) {
        is WakeWordState.Starting,
        is WakeWordState.Listening,
        is WakeWordState.Detected,
        -> true

        is WakeWordState.Off,
        is WakeWordState.Stopping,
        is WakeWordState.Unavailable,
        is WakeWordState.PermissionDenied,
        is WakeWordState.Failed,
        -> false
    }

/** Short human-readable label for logs and diagnostics. */
fun WakeWordState.describe(): String = when (this) {
    is WakeWordState.Off -> "Off"
    is WakeWordState.Starting -> "Starting"
    is WakeWordState.Listening -> "Listening"
    is WakeWordState.Detected -> "Detected"
    is WakeWordState.Stopping -> "Stopping"
    is WakeWordState.Unavailable -> "Unavailable: $reason"
    is WakeWordState.PermissionDenied -> "Permission denied"
    is WakeWordState.Failed -> "Failed: $reason"
}
