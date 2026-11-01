package com.example.hermestalk.rtvi

/** Events that drive [ConnectionMachine]. */
sealed interface ConnectionEvent {
    /** The user asked to start a connection. */
    data object ConnectRequested : ConnectionEvent

    /** The liveness probe is about to run. */
    data object ProbeStarted : ConnectionEvent

    /** The liveness probe succeeded. */
    data object ProbeSucceeded : ConnectionEvent

    /** The liveness probe failed. */
    data class ProbeFailed(val reason: FailureReason, val detail: String) : ConnectionEvent

    /** WebRTC negotiation is about to start. */
    data object NegotiationStarted : ConnectionEvent

    /** The WebRTC data channel is open. */
    data object TransportConnected : ConnectionEvent

    /** The bot announced readiness. */
    data object BotReady : ConnectionEvent

    /** The transport closed. */
    data object TransportDisconnected : ConnectionEvent

    /** The transport failed. */
    data class TransportFailed(val reason: FailureReason, val detail: String) : ConnectionEvent

    /** The relay reported a backend error. */
    data class BackendError(val detail: String) : ConnectionEvent

    /** The user asked to disconnect. */
    data object DisconnectRequested : ConnectionEvent

    /** A retry is scheduled for the given attempt number. */
    data class RetryScheduled(val attempt: Int) : ConnectionEvent

    /** No retries remain. */
    data object RetryExhausted : ConnectionEvent
}

/** Side effects the caller must perform after a transition. */
sealed interface ConnectionEffect {
    /** Release the current Pipecat client and its native resources. */
    data object ReleaseClient : ConnectionEffect

    /** Cancel any pending retry coroutine. */
    data object CancelRetry : ConnectionEffect

    /** Schedule another connection attempt. */
    data object ScheduleRetry : ConnectionEffect
}

/** The result of reducing an event against a state. */
data class Transition(
    val state: ConnectionState,
    val effects: List<ConnectionEffect> = emptyList(),
)

/**
 * Deterministic, pure reducer for the connection lifecycle. It has no Android
 * or coroutine dependencies, so the session and the UI can both reason about
 * transitions without side effects.
 */
object ConnectionMachine {

    /**
     * Apply [event] to [current]. Unknown or irrelevant combinations return the
     * current state unchanged and never throw.
     *
     * A backend error while connected or ready is treated as a terminal failure
     * for this attempt ([FailureReason.BackendError], not retryable) rather than
     * a transport drop: the relay explicitly refused the session, so retrying
     * the same handshake immediately is unlikely to help. The failed state stays
     * visible to the user, and a manual connect starts a fresh attempt.
     */
    fun reduce(current: ConnectionState, event: ConnectionEvent): Transition {
        val transition = when (event) {
            ConnectionEvent.ConnectRequested -> when (current) {
                is ConnectionState.Disconnected,
                is ConnectionState.Failed
                -> Transition(ConnectionState.Connecting(ConnectingPhase.Probing))

                else -> Transition(current)
            }

            ConnectionEvent.ProbeStarted -> when (current) {
                is ConnectionState.Connecting,
                is ConnectionState.Reconnecting
                -> Transition(ConnectionState.Connecting(ConnectingPhase.Probing))

                else -> Transition(current)
            }

            ConnectionEvent.ProbeSucceeded -> when (current) {
                is ConnectionState.Connecting ->
                    Transition(ConnectionState.Connecting(ConnectingPhase.Negotiating))

                else -> Transition(current)
            }

            is ConnectionEvent.ProbeFailed -> when (current) {
                is ConnectionState.Connecting -> failure(
                    event.reason,
                    event.detail,
                    retryable = isRetryable(event.reason),
                )

                else -> Transition(current)
            }

            ConnectionEvent.NegotiationStarted -> when (current) {
                is ConnectionState.Connecting ->
                    Transition(ConnectionState.Connecting(ConnectingPhase.Negotiating))

                else -> Transition(current)
            }

            ConnectionEvent.TransportConnected -> when (current) {
                is ConnectionState.Connecting,
                is ConnectionState.Reconnecting
                -> Transition(ConnectionState.Connected)

                else -> Transition(current)
            }

            ConnectionEvent.BotReady -> when (current) {
                is ConnectionState.Connected -> Transition(ConnectionState.Ready)
                else -> Transition(current)
            }

            ConnectionEvent.TransportDisconnected -> when (current) {
                is ConnectionState.Disconnected -> Transition(ConnectionState.Disconnected)

                is ConnectionState.Connecting,
                is ConnectionState.Reconnecting,
                is ConnectionState.Connected,
                is ConnectionState.Ready
                -> failure(
                    FailureReason.TransportClosed,
                    "The transport closed unexpectedly",
                    retryable = true,
                )

                is ConnectionState.Failed -> Transition(current)
            }

            is ConnectionEvent.TransportFailed -> when (current) {
                is ConnectionState.Disconnected -> Transition(current)
                else -> failure(
                    event.reason,
                    event.detail,
                    retryable = isRetryable(event.reason),
                )
            }

            is ConnectionEvent.BackendError -> when (current) {
                is ConnectionState.Connecting,
                is ConnectionState.Connected,
                is ConnectionState.Ready
                -> failure(FailureReason.BackendError, event.detail, retryable = false)

                else -> Transition(current)
            }

            ConnectionEvent.DisconnectRequested -> when (current) {
                is ConnectionState.Disconnected -> Transition(ConnectionState.Disconnected)
                else -> Transition(
                    ConnectionState.Disconnected,
                    listOf(ConnectionEffect.ReleaseClient, ConnectionEffect.CancelRetry),
                )
            }

            is ConnectionEvent.RetryScheduled -> when (current) {
                is ConnectionState.Failed ->
                    Transition(ConnectionState.Reconnecting(event.attempt, current.reason))

                is ConnectionState.Reconnecting ->
                    Transition(ConnectionState.Reconnecting(event.attempt, current.reason))

                else -> Transition(current)
            }

            ConnectionEvent.RetryExhausted -> when (current) {
                is ConnectionState.Reconnecting ->
                    Transition(
                        ConnectionState.Failed(
                            current.reason,
                            "Retry limit reached",
                            retryable = false,
                        ),
                    )

                is ConnectionState.Failed -> Transition(current.copy(retryable = false))
                else -> Transition(current)
            }
        }
        return transition
    }

    private fun failure(
        reason: FailureReason,
        detail: String,
        retryable: Boolean,
    ): Transition {
        val effects = if (retryable) listOf(ConnectionEffect.ScheduleRetry) else emptyList()
        return Transition(
            ConnectionState.Failed(reason, detail, retryable),
            effects,
        )
    }

    private fun isRetryable(reason: FailureReason): Boolean = when (reason) {
        FailureReason.Unreachable,
        FailureReason.NotReady,
        FailureReason.HandshakeFailed,
        FailureReason.TransportClosed,
        -> true

        FailureReason.Unauthorized,
        FailureReason.PermissionDenied,
        FailureReason.BackendError,
        FailureReason.InvalidConfiguration,
        FailureReason.Unexpected,
        -> false
    }
}
