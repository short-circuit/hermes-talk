package com.example.hermestalk.call

/** Deterministic call lifecycle states surfaced to the UI. */
sealed interface CallState {
    /** No call is running. */
    data object Idle : CallState

    /** The call is waiting for the microphone runtime permission. */
    data object AwaitingMicPermission : CallState

    /** The call is starting the foreground service or connecting the session. */
    data class Starting(val phase: StartPhase) : CallState

    /** The session is ready and audio is flowing. */
    data object Active : CallState

    /** The session lost its transport and is retrying. */
    data class Reconnecting(val attempt: Int, val detail: String) : CallState

    /** The user ended the call and cleanup is running. */
    data object Ending : CallState

    /** The call ended normally. */
    data class Ended(val durationSeconds: Long) : CallState

    /** The call failed and cleanup has run. */
    data class Failed(val reason: CallFailureReason, val detail: String) : CallState
}

/** Phases within [CallState.Starting]. */
enum class StartPhase { ServiceStart, Connecting }

/** Stable categories for call failures. */
enum class CallFailureReason {
    MicrophonePermissionDenied,
    MicrophoneUnavailable,
    RelayUnreachable,
    RelayRejected,
    HandshakeFailed,
    AudioFocusLost,
    Unexpected,
}

/** Audio routes the call screen can offer. */
enum class AudioRoute { Earpiece, Speaker, WiredHeadset, Bluetooth }

/** Result of an audio-focus request or a focus change callback. */
enum class AudioFocusOutcome { Gained, Lost, LostTransient, Duck, Denied }

/** Call controls shown by the in-call screen. */
data class CallControls(
    val userMuted: Boolean = false,
    val muted: Boolean = false,
    val route: AudioRoute = AudioRoute.Earpiece,
    val availableRoutes: List<AudioRoute> = listOf(AudioRoute.Earpiece),
    val durationSeconds: Long = 0,
    val levels: List<Float> = emptyList(),
    val micActive: Boolean = false,
    val batteryOptimisationNeeded: Boolean = false,
    val audioFocusHeld: Boolean = false,
)

/** Complete UI state for the call screen. */
data class CallUiState(
    val state: CallState = CallState.Idle,
    val controls: CallControls = CallControls(),
)

/** True when the call is starting, active, reconnecting or ending. */
val CallState.isLive: Boolean
    get() = when (this) {
        is CallState.Starting,
        is CallState.Active,
        is CallState.Reconnecting,
        is CallState.Ending,
        -> true

        is CallState.Idle,
        is CallState.AwaitingMicPermission,
        is CallState.Ended,
        is CallState.Failed,
        -> false
    }

/** Short human-readable label for logs and diagnostics. */
fun CallState.describe(): String = when (this) {
    is CallState.Idle -> "Idle"
    is CallState.AwaitingMicPermission -> "Awaiting microphone permission"
    is CallState.Starting -> when (phase) {
        StartPhase.ServiceStart -> "Starting (service)"
        StartPhase.Connecting -> "Starting (connecting)"
    }
    is CallState.Active -> "Active"
    is CallState.Reconnecting -> "Reconnecting (attempt $attempt)"
    is CallState.Ending -> "Ending"
    is CallState.Ended -> "Ended after ${durationSeconds}s"
    is CallState.Failed -> "Failed: $reason"
}
