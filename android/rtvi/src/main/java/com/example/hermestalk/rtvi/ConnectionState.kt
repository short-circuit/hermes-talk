package com.example.hermestalk.rtvi

/** Connection states surfaced to the UI. */
sealed interface ConnectionState {
    /** No client is connected and no connection attempt is running. */
    data object Disconnected : ConnectionState

    /** A connection attempt is in progress. */
    data class Connecting(val phase: ConnectingPhase) : ConnectionState

    /** The WebRTC transport is connected but the bot has not announced readiness. */
    data object Connected : ConnectionState

    /** The bot announced readiness over the RTVI data channel. */
    data object Ready : ConnectionState

    /** A retry is scheduled after a retryable failure. */
    data class Reconnecting(val attempt: Int, val reason: FailureReason) : ConnectionState

    /** The connection attempt or active connection failed. */
    data class Failed(val reason: FailureReason, val detail: String, val retryable: Boolean) :
        ConnectionState
}

/** Phases within [ConnectionState.Connecting]. */
enum class ConnectingPhase { Probing, Negotiating }

/** Stable categories for connection failures. */
enum class FailureReason {
    Unreachable,
    Unauthorized,
    NotReady,
    HandshakeFailed,
    PermissionDenied,
    BackendError,
    TransportClosed,
    InvalidConfiguration,
    Unexpected,
}

/**
 * True when the state represents an active or in-flight connection. Failed and
 * Disconnected states are not active.
 */
val ConnectionState.isActive: Boolean
    get() = when (this) {
        is ConnectionState.Disconnected -> false
        is ConnectionState.Connecting -> true
        is ConnectionState.Connected -> true
        is ConnectionState.Ready -> true
        is ConnectionState.Reconnecting -> true
        is ConnectionState.Failed -> false
    }

/** Short human-readable label for logs and diagnostics. */
fun ConnectionState.describe(): String = when (this) {
    is ConnectionState.Disconnected -> "Disconnected"
    is ConnectionState.Connecting -> when (phase) {
        ConnectingPhase.Probing -> "Connecting (probing)"
        ConnectingPhase.Negotiating -> "Connecting (negotiating)"
    }
    is ConnectionState.Connected -> "Connected"
    is ConnectionState.Ready -> "Ready"
    is ConnectionState.Reconnecting -> "Reconnecting (attempt $attempt)"
    is ConnectionState.Failed -> "Failed: $reason"
}
