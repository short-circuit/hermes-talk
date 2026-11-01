package com.example.hermestalk.rtvi

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ConnectionMachineTest {

    private fun reduce(state: ConnectionState, event: ConnectionEvent): Transition =
        ConnectionMachine.reduce(state, event)

    private fun run(vararg events: ConnectionEvent): ConnectionState {
        var state: ConnectionState = ConnectionState.Disconnected
        for (event in events) {
            state = ConnectionMachine.reduce(state, event).state
        }
        return state
    }

    @Test
    fun connectFromDisconnectedStartsProbing() {
        val transition = reduce(ConnectionState.Disconnected, ConnectionEvent.ConnectRequested)
        assertEquals(ConnectionState.Connecting(ConnectingPhase.Probing), transition.state)
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun connectFromFailedStartsProbing() {
        val failed = ConnectionState.Failed(FailureReason.Unreachable, "offline", retryable = true)
        val transition = reduce(failed, ConnectionEvent.ConnectRequested)
        assertEquals(ConnectionState.Connecting(ConnectingPhase.Probing), transition.state)
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun connectWhileConnectingIsIgnored() {
        val current = ConnectionState.Connecting(ConnectingPhase.Negotiating)
        val transition = reduce(current, ConnectionEvent.ConnectRequested)
        assertEquals(current, transition.state)
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun connectWhileConnectedIsIgnored() {
        val transition = reduce(ConnectionState.Connected, ConnectionEvent.ConnectRequested)
        assertEquals(ConnectionState.Connected, transition.state)
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun connectWhileReadyIsIgnored() {
        val transition = reduce(ConnectionState.Ready, ConnectionEvent.ConnectRequested)
        assertEquals(ConnectionState.Ready, transition.state)
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun connectWhileReconnectingIsIgnored() {
        val current = ConnectionState.Reconnecting(2, FailureReason.Unreachable)
        val transition = reduce(current, ConnectionEvent.ConnectRequested)
        assertEquals(current, transition.state)
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun probeFailureUnauthorizedIsNotRetryable() {
        val state = run(
            ConnectionEvent.ConnectRequested,
            ConnectionEvent.ProbeStarted,
            ConnectionEvent.ProbeFailed(FailureReason.Unauthorized, "bad token"),
        )
        assertEquals(
            ConnectionState.Failed(FailureReason.Unauthorized, "bad token", retryable = false),
            state,
        )
        val transition = reduce(
            ConnectionState.Connecting(ConnectingPhase.Probing),
            ConnectionEvent.ProbeFailed(FailureReason.Unauthorized, "bad token"),
        )
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun probeFailureUnreachableIsRetryable() {
        val transition = reduce(
            ConnectionState.Connecting(ConnectingPhase.Probing),
            ConnectionEvent.ProbeFailed(FailureReason.Unreachable, "offline"),
        )
        assertEquals(
            ConnectionState.Failed(FailureReason.Unreachable, "offline", retryable = true),
            transition.state,
        )
        assertEquals(listOf(ConnectionEffect.ScheduleRetry), transition.effects)
    }

    @Test
    fun probeFailureNotReadyIsRetryable() {
        val transition = reduce(
            ConnectionState.Connecting(ConnectingPhase.Probing),
            ConnectionEvent.ProbeFailed(FailureReason.NotReady, "warming up"),
        )
        assertEquals(
            ConnectionState.Failed(FailureReason.NotReady, "warming up", retryable = true),
            transition.state,
        )
        assertEquals(listOf(ConnectionEffect.ScheduleRetry), transition.effects)
    }

    @Test
    fun probeFailureUnexpectedIsNotRetryable() {
        val transition = reduce(
            ConnectionState.Connecting(ConnectingPhase.Probing),
            ConnectionEvent.ProbeFailed(FailureReason.Unexpected, "boom"),
        )
        assertEquals(
            ConnectionState.Failed(FailureReason.Unexpected, "boom", retryable = false),
            transition.state,
        )
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun probeSuccessStartsNegotiation() {
        val state = run(
            ConnectionEvent.ConnectRequested,
            ConnectionEvent.ProbeStarted,
            ConnectionEvent.ProbeSucceeded,
        )
        assertEquals(ConnectionState.Connecting(ConnectingPhase.Negotiating), state)
    }

    @Test
    fun transportConnectedMovesToConnected() {
        val state = run(
            ConnectionEvent.ConnectRequested,
            ConnectionEvent.ProbeStarted,
            ConnectionEvent.ProbeSucceeded,
            ConnectionEvent.TransportConnected,
        )
        assertEquals(ConnectionState.Connected, state)
    }

    @Test
    fun transportHandshakeFailureIsRetryable() {
        val transition = reduce(
            ConnectionState.Connecting(ConnectingPhase.Negotiating),
            ConnectionEvent.TransportFailed(FailureReason.HandshakeFailed, "sdp rejected"),
        )
        assertEquals(
            ConnectionState.Failed(
                FailureReason.HandshakeFailed,
                "sdp rejected",
                retryable = true,
            ),
            transition.state,
        )
        assertEquals(listOf(ConnectionEffect.ScheduleRetry), transition.effects)
    }

    @Test
    fun transportUnauthorizedIsNotRetryable() {
        val transition = reduce(
            ConnectionState.Connecting(ConnectingPhase.Negotiating),
            ConnectionEvent.TransportFailed(FailureReason.Unauthorized, "401"),
        )
        assertEquals(
            ConnectionState.Failed(FailureReason.Unauthorized, "401", retryable = false),
            transition.state,
        )
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun transportPermissionDeniedIsNotRetryable() {
        val transition = reduce(
            ConnectionState.Connecting(ConnectingPhase.Negotiating),
            ConnectionEvent.TransportFailed(FailureReason.PermissionDenied, "mic"),
        )
        assertEquals(
            ConnectionState.Failed(FailureReason.PermissionDenied, "mic", retryable = false),
            transition.state,
        )
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun transportDisconnectFromConnectedFailsAndSchedulesRetry() {
        val transition = reduce(ConnectionState.Connected, ConnectionEvent.TransportDisconnected)
        assertEquals(
            ConnectionState.Failed(FailureReason.TransportClosed, "The transport closed unexpectedly", retryable = true),
            transition.state,
        )
        assertEquals(listOf(ConnectionEffect.ScheduleRetry), transition.effects)
    }

    @Test
    fun transportDisconnectFromReadyFailsAndSchedulesRetry() {
        val transition = reduce(ConnectionState.Ready, ConnectionEvent.TransportDisconnected)
        assertEquals(
            ConnectionState.Failed(FailureReason.TransportClosed, "The transport closed unexpectedly", retryable = true),
            transition.state,
        )
        assertEquals(listOf(ConnectionEffect.ScheduleRetry), transition.effects)
    }

    @Test
    fun transportDisconnectFromDisconnectedIsNoOp() {
        val transition = reduce(ConnectionState.Disconnected, ConnectionEvent.TransportDisconnected)
        assertEquals(ConnectionState.Disconnected, transition.state)
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun backendErrorFromConnectedIsTerminalFailure() {
        val transition = reduce(
            ConnectionState.Connected,
            ConnectionEvent.BackendError("relay refused session"),
        )
        assertEquals(
            ConnectionState.Failed(
                FailureReason.BackendError,
                "relay refused session",
                retryable = false,
            ),
            transition.state,
        )
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun backendErrorFromReadyIsTerminalFailure() {
        val transition = reduce(
            ConnectionState.Ready,
            ConnectionEvent.BackendError("relay refused session"),
        )
        assertEquals(
            ConnectionState.Failed(
                FailureReason.BackendError,
                "relay refused session",
                retryable = false,
            ),
            transition.state,
        )
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun disconnectFromConnectedReleasesAndCancelsRetry() {
        val transition = reduce(ConnectionState.Connected, ConnectionEvent.DisconnectRequested)
        assertEquals(ConnectionState.Disconnected, transition.state)
        assertEquals(
            listOf(ConnectionEffect.ReleaseClient, ConnectionEffect.CancelRetry),
            transition.effects,
        )
    }

    @Test
    fun disconnectFromFailedReleasesAndCancelsRetry() {
        val failed = ConnectionState.Failed(FailureReason.Unreachable, "offline", retryable = true)
        val transition = reduce(failed, ConnectionEvent.DisconnectRequested)
        assertEquals(ConnectionState.Disconnected, transition.state)
        assertEquals(
            listOf(ConnectionEffect.ReleaseClient, ConnectionEffect.CancelRetry),
            transition.effects,
        )
    }

    @Test
    fun disconnectFromDisconnectedIsNoOp() {
        val transition = reduce(ConnectionState.Disconnected, ConnectionEvent.DisconnectRequested)
        assertEquals(ConnectionState.Disconnected, transition.state)
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun retryScheduledMovesFailedToReconnecting() {
        val failed = ConnectionState.Failed(FailureReason.Unreachable, "offline", retryable = true)
        val transition = reduce(failed, ConnectionEvent.RetryScheduled(2))
        assertEquals(ConnectionState.Reconnecting(2, FailureReason.Unreachable), transition.state)
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun retryExhaustedBecomesNonRetryableFailure() {
        val transition = reduce(
            ConnectionState.Reconnecting(3, FailureReason.HandshakeFailed),
            ConnectionEvent.RetryExhausted,
        )
        assertEquals(
            ConnectionState.Failed(
                FailureReason.HandshakeFailed,
                "Retry limit reached",
                retryable = false,
            ),
            transition.state,
        )
        assertFalse(transition.state.let { it is ConnectionState.Failed && it.retryable })
    }

    @Test
    fun botReadyOnlyAppliesFromConnected() {
        assertEquals(
            ConnectionState.Ready,
            reduce(ConnectionState.Connected, ConnectionEvent.BotReady).state,
        )
        assertEquals(
            ConnectionState.Connecting(ConnectingPhase.Negotiating),
            reduce(
                ConnectionState.Connecting(ConnectingPhase.Negotiating),
                ConnectionEvent.BotReady,
            ).state,
        )
        assertEquals(
            ConnectionState.Ready,
            reduce(ConnectionState.Ready, ConnectionEvent.BotReady).state,
        )
    }
}
