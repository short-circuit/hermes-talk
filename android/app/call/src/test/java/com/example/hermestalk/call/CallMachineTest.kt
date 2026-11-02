package com.example.hermestalk.call

import com.example.hermestalk.rtvi.ConnectionState
import com.example.hermestalk.rtvi.ConnectingPhase
import com.example.hermestalk.rtvi.FailureReason
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CallMachineTest {

    private fun ui(
        state: CallState = CallState.Idle,
        controls: CallControls = CallControls(),
    ): CallUiState = CallUiState(state, controls)

    private fun reduce(
        state: CallState,
        event: CallEvent,
        controls: CallControls = CallControls(),
    ): CallTransition = CallMachine.reduce(ui(state, controls), event)

    private fun active(controls: CallControls = CallControls()): CallUiState =
        ui(CallState.Active, controls)

    @Test
    fun startFromIdleRequestsPermission() {
        val transition = reduce(CallState.Idle, CallEvent.StartRequested)
        assertEquals(CallState.AwaitingMicPermission, transition.state)
        assertEquals(listOf(CallEffect.RequestMicPermission), transition.effects)
        assertFalse(transition.state.isLive)
    }

    @Test
    fun startFromEndedAndFailedRequestsPermission() {
        val fromEnded = reduce(CallState.Ended(42), CallEvent.StartRequested)
        assertEquals(CallState.AwaitingMicPermission, fromEnded.state)
        assertEquals(listOf(CallEffect.RequestMicPermission), fromEnded.effects)

        val fromFailed = reduce(
            CallState.Failed(CallFailureReason.RelayUnreachable, "offline"),
            CallEvent.StartRequested,
        )
        assertEquals(CallState.AwaitingMicPermission, fromFailed.state)
        assertEquals(listOf(CallEffect.RequestMicPermission), fromFailed.effects)
    }

    @Test
    fun startWhileActiveIsIgnored() {
        val transition = reduce(CallState.Active, CallEvent.StartRequested)
        assertEquals(CallState.Active, transition.state)
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun permissionGrantedStartsServiceFocusAndTimer() {
        val transition = reduce(
            CallState.AwaitingMicPermission,
            CallEvent.MicPermissionGranted,
        )
        assertEquals(CallState.Starting(StartPhase.ServiceStart), transition.state)
        assertEquals(
            listOf(
                CallEffect.StartForegroundService,
                CallEffect.AcquireAudioFocus,
                CallEffect.StartTimer,
            ),
            transition.effects,
        )
    }

    @Test
    fun permissionDeniedFailsAndCleansUpWithoutLiveService() {
        val transition = reduce(
            CallState.AwaitingMicPermission,
            CallEvent.MicPermissionDenied,
        )
        assertEquals(
            CallState.Failed(
                CallFailureReason.MicrophonePermissionDenied,
                "Microphone permission denied",
            ),
            transition.state,
        )
        assertFalse(transition.state.isLive)
        assertTrue(transition.controls.muted)
        assertFalse(transition.controls.micActive)
        assertEquals(
            listOf(
                CallEffect.StopForegroundService,
                CallEffect.AbandonAudioFocus,
                CallEffect.StopTimer,
                CallEffect.DisconnectAndReleaseSession,
            ),
            transition.effects,
        )
    }

    @Test
    fun permissionRevokedFailsAndCleansUp() {
        val transition = reduce(CallState.Active, CallEvent.MicPermissionRevoked)
        assertEquals(CallState.Failed(CallFailureReason.MicrophonePermissionDenied, "Microphone permission revoked"), transition.state)
        assertTrue(transition.effects.contains(CallEffect.StopForegroundService))
        assertTrue(transition.effects.contains(CallEffect.DisconnectAndReleaseSession))
        assertTrue(transition.effects.contains(CallEffect.StopTimer))
    }

    @Test
    fun serviceStartedConnectsSession() {
        val transition = reduce(
            CallState.Starting(StartPhase.ServiceStart),
            CallEvent.ServiceStarted,
        )
        assertEquals(CallState.Starting(StartPhase.Connecting), transition.state)
        assertEquals(listOf(CallEffect.ConnectSession), transition.effects)
    }

    @Test
    fun connectedKeepsStartingAndReadyActivates() {
        val connected = reduce(
            CallState.Starting(StartPhase.Connecting),
            CallEvent.SessionStateChanged(ConnectionState.Connected),
        )
        assertEquals(CallState.Starting(StartPhase.Connecting), connected.state)

        val ready = reduce(
            CallState.Starting(StartPhase.Connecting),
            CallEvent.SessionStateChanged(ConnectionState.Ready),
            CallControls(userMuted = true),
        )
        assertEquals(CallState.Active, ready.state)
        assertTrue(ready.controls.muted)
        assertEquals(
            listOf(
                CallEffect.ApplyMicState,
                CallEffect.ApplyAudioRoute,
                CallEffect.UpdateForegroundNotification,
            ),
            ready.effects,
        )
    }

    @Test
    fun reconnectingKeepsCallAliveAndUpdatesNotification() {
        val transition = reduce(
            CallState.Active,
            CallEvent.SessionStateChanged(
                ConnectionState.Reconnecting(2, FailureReason.Unreachable),
            ),
        )
        assertEquals(CallState.Reconnecting(2, "Relay reconnecting: Unreachable"), transition.state)
        assertTrue(transition.state.isLive)
        assertEquals(listOf(CallEffect.UpdateForegroundNotification), transition.effects)
    }

    @Test
    fun disconnectedWhileLiveFailsWithCleanup() {
        val transition = reduce(
            CallState.Active,
            CallEvent.SessionStateChanged(ConnectionState.Disconnected),
        )
        assertEquals(
            CallState.Failed(CallFailureReason.RelayUnreachable, "Relay connection closed"),
            transition.state,
        )
        assertEquals(
            listOf(
                CallEffect.DisconnectAndReleaseSession,
                CallEffect.StopForegroundService,
                CallEffect.AbandonAudioFocus,
                CallEffect.StopTimer,
                CallEffect.ClearCommunicationDevice,
                CallEffect.RestoreAudioMode,
            ),
            transition.effects,
        )
        assertFalse(transition.state.isLive)
    }

    @Test
    fun disconnectedOutsideCallIsIgnored() {
        val idle = reduce(CallState.Idle, CallEvent.SessionStateChanged(ConnectionState.Disconnected))
        assertEquals(CallState.Idle, idle.state)
        assertTrue(idle.effects.isEmpty())

        val ended = reduce(CallState.Ended(5), CallEvent.SessionStateChanged(ConnectionState.Disconnected))
        assertEquals(CallState.Ended(5), ended.state)
        assertTrue(ended.effects.isEmpty())
    }

    @Test
    fun failedConnectionMapsReasonAndCleansUp() {
        val transition = reduce(
            CallState.Active,
            CallEvent.SessionStateChanged(
                ConnectionState.Failed(FailureReason.TlsRejected, "pin mismatch", retryable = false),
            ),
        )
        assertEquals(
            CallState.Failed(CallFailureReason.RelayRejected, "pin mismatch"),
            transition.state,
        )
        assertEquals(
            listOf(
                CallEffect.DisconnectAndReleaseSession,
                CallEffect.StopForegroundService,
                CallEffect.AbandonAudioFocus,
                CallEffect.StopTimer,
                CallEffect.ClearCommunicationDevice,
                CallEffect.RestoreAudioMode,
            ),
            transition.effects,
        )
    }

    @Test
    fun sessionFailureWhileLiveCleansUp() {
        val transition = reduce(
            CallState.Reconnecting(1, "retry"),
            CallEvent.SessionFailed(CallFailureReason.HandshakeFailed, "bad offer"),
        )
        assertEquals(
            CallState.Failed(CallFailureReason.HandshakeFailed, "bad offer"),
            transition.state,
        )
        assertTrue(transition.effects.contains(CallEffect.DisconnectAndReleaseSession))
        assertTrue(transition.effects.contains(CallEffect.StopForegroundService))
    }

    @Test
    fun sessionFailureOutsideCallIsIgnored() {
        val transition = reduce(
            CallState.Idle,
            CallEvent.SessionFailed(CallFailureReason.Unexpected, "ignored"),
        )
        assertEquals(CallState.Idle, transition.state)
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun muteTogglesPreferenceWithoutLeavingActive() {
        val muted = reduce(CallState.Active, CallEvent.MuteRequested)
        assertEquals(CallState.Active, muted.state)
        assertTrue(muted.controls.userMuted)
        assertTrue(muted.controls.muted)
        assertEquals(
            listOf(CallEffect.ApplyMicState, CallEffect.UpdateForegroundNotification),
            muted.effects,
        )

        val unmuted = CallMachine.reduce(
            ui(CallState.Active, muted.controls),
            CallEvent.UnmuteRequested,
        )
        assertEquals(CallState.Active, unmuted.state)
        assertFalse(unmuted.controls.userMuted)
        assertFalse(unmuted.controls.muted)
        assertEquals(
            listOf(CallEffect.ApplyMicState, CallEffect.UpdateForegroundNotification),
            unmuted.effects,
        )
    }

    @Test
    fun muteWorksWhileReconnectingAndIsIgnoredWhenIdle() {
        val reconnecting = reduce(CallState.Reconnecting(1, "retry"), CallEvent.MuteRequested)
        assertTrue(reconnecting.controls.muted)
        assertEquals(CallState.Reconnecting(1, "retry"), reconnecting.state)

        val idle = reduce(CallState.Idle, CallEvent.MuteRequested)
        assertFalse(idle.controls.muted)
        assertTrue(idle.effects.isEmpty())
    }

    @Test
    fun audioFocusLossMutesButKeepsCallActive() {
        val lost = reduce(CallState.Active, CallEvent.AudioFocusChanged(AudioFocusOutcome.Lost))
        assertEquals(CallState.Active, lost.state)
        assertTrue(lost.controls.muted)
        assertFalse(lost.controls.audioFocusHeld)
        assertEquals(listOf(CallEffect.ApplyMicState), lost.effects)

        val transient = reduce(
            CallState.Active,
            CallEvent.AudioFocusChanged(AudioFocusOutcome.LostTransient),
        )
        assertEquals(CallState.Active, transient.state)
        assertTrue(transient.controls.muted)
    }

    @Test
    fun audioFocusGainedAndDeniedUpdateFlags() {
        val gained = reduce(
            CallState.Active,
            CallEvent.AudioFocusChanged(AudioFocusOutcome.Gained),
            CallControls(userMuted = true),
        )
        assertEquals(CallState.Active, gained.state)
        assertTrue(gained.controls.muted)
        assertTrue(gained.controls.audioFocusHeld)
        assertEquals(listOf(CallEffect.ApplyMicState), gained.effects)

        val denied = reduce(CallState.Active, CallEvent.AudioFocusChanged(AudioFocusOutcome.Denied))
        assertEquals(CallState.Active, denied.state)
        assertFalse(denied.controls.audioFocusHeld)
        assertTrue(denied.effects.isEmpty())
    }

    @Test
    fun routeSelectionOnlyAcceptsAvailableRoutes() {
        val available = listOf(AudioRoute.Earpiece, AudioRoute.Speaker, AudioRoute.Bluetooth)
        val selected = reduce(
            CallState.Active,
            CallEvent.RouteSelected(AudioRoute.Bluetooth),
            CallControls(availableRoutes = available),
        )
        assertEquals(AudioRoute.Bluetooth, selected.controls.route)
        assertEquals(listOf(CallEffect.ApplyAudioRoute), selected.effects)

        val rejected = reduce(
            CallState.Active,
            CallEvent.RouteSelected(AudioRoute.WiredHeadset),
            CallControls(availableRoutes = available),
        )
        assertEquals(AudioRoute.Earpiece, rejected.controls.route)
        assertTrue(rejected.effects.isEmpty())
    }

    @Test
    fun routeSelectionWorksWhileStarting() {
        val transition = reduce(
            CallState.Starting(StartPhase.Connecting),
            CallEvent.RouteSelected(AudioRoute.Speaker),
            CallControls(availableRoutes = listOf(AudioRoute.Earpiece, AudioRoute.Speaker)),
        )
        assertEquals(CallState.Starting(StartPhase.Connecting), transition.state)
        assertEquals(AudioRoute.Speaker, transition.controls.route)
    }

    @Test
    fun deviceChangeRemovingHeadsetAppliesRouteWithoutDisconnect() {
        val transition = reduce(
            CallState.Active,
            CallEvent.AudioDevicesChanged(listOf(AudioRoute.Earpiece, AudioRoute.Speaker)),
            CallControls(
                route = AudioRoute.Bluetooth,
                availableRoutes = listOf(
                    AudioRoute.Earpiece,
                    AudioRoute.Speaker,
                    AudioRoute.Bluetooth,
                ),
            ),
        )
        assertEquals(CallState.Active, transition.state)
        assertEquals(AudioRoute.Earpiece, transition.controls.route)
        assertEquals(listOf(CallEffect.ApplyAudioRoute), transition.effects)
        assertFalse(transition.effects.contains(CallEffect.DisconnectAndReleaseSession))
    }

    @Test
    fun deviceChangeKeepingCurrentRouteDoesNotDisconnect() {
        val transition = reduce(
            CallState.Active,
            CallEvent.AudioDevicesChanged(listOf(AudioRoute.Earpiece, AudioRoute.Speaker)),
            CallControls(route = AudioRoute.Speaker),
        )
        assertEquals(AudioRoute.Speaker, transition.controls.route)
        assertEquals(listOf(CallEffect.ApplyAudioRoute), transition.effects)
        assertFalse(transition.effects.contains(CallEffect.DisconnectAndReleaseSession))
    }

    @Test
    fun endRequestedEmitsExactlyOneCleanup() {
        val transition = reduce(CallState.Active, CallEvent.EndRequested)
        assertEquals(CallState.Ending, transition.state)
        assertTrue(transition.controls.muted)
        assertEquals(
            listOf(
                CallEffect.StopTimer,
                CallEffect.UpdateForegroundNotification,
                CallEffect.DisconnectAndReleaseSession,
                CallEffect.StopForegroundService,
                CallEffect.AbandonAudioFocus,
                CallEffect.ClearCommunicationDevice,
                CallEffect.RestoreAudioMode,
                CallEffect.ApplyMicState,
            ),
            transition.effects,
        )
        assertEquals(1, transition.effects.count { it == CallEffect.DisconnectAndReleaseSession })
        assertEquals(1, transition.effects.count { it == CallEffect.StopForegroundService })
    }

    @Test
    fun doubleEndProducesNoDuplicateEffects() {
        val ending = reduce(CallState.Active, CallEvent.EndRequested)
        val second = CallMachine.reduce(
            ui(ending.state, ending.controls),
            CallEvent.EndRequested,
        )
        assertEquals(CallState.Ending, second.state)
        assertTrue(second.effects.isEmpty())
    }

    @Test
    fun endFromIdleOrEndedIsIgnored() {
        val idle = reduce(CallState.Idle, CallEvent.EndRequested)
        assertEquals(CallState.Idle, idle.state)
        assertTrue(idle.effects.isEmpty())

        val ended = reduce(CallState.Ended(10), CallEvent.EndRequested)
        assertEquals(CallState.Ended(10), ended.state)
        assertTrue(ended.effects.isEmpty())
    }

    @Test
    fun endCompletedResetsControlsAndKeepsDuration() {
        val ending = CallUiState(
            CallState.Ending,
            CallControls(
                userMuted = true,
                muted = true,
                route = AudioRoute.Bluetooth,
                durationSeconds = 75,
                levels = listOf(0.2f, 0.5f),
            ),
        )
        val transition = CallMachine.reduce(ending, CallEvent.EndCompleted)
        assertEquals(CallState.Ended(75), transition.state)
        assertFalse(transition.controls.muted)
        assertFalse(transition.controls.userMuted)
        assertEquals(AudioRoute.Earpiece, transition.controls.route)
        assertEquals(75L, transition.controls.durationSeconds)
        assertTrue(transition.controls.levels.isEmpty())
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun tickUpdatesDurationOnlyWhileActive() {
        val activeTick = reduce(CallState.Active, CallEvent.Tick(42))
        assertEquals(CallState.Active, activeTick.state)
        assertEquals(42L, activeTick.controls.durationSeconds)

        val reconnectTick = reduce(CallState.Reconnecting(1, "retry"), CallEvent.Tick(42))
        assertEquals(0L, reconnectTick.controls.durationSeconds)
    }

    @Test
    fun acknowledgeEndedReturnsToIdle() {
        val transition = reduce(CallState.Ended(12), CallEvent.AcknowledgeEnded)
        assertEquals(CallState.Idle, transition.state)
        assertEquals(CallControls(), transition.controls)
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun unknownCombinationsAreIgnored() {
        val transition = reduce(CallState.Idle, CallEvent.ServiceStarted)
        assertEquals(CallState.Idle, transition.state)
        assertTrue(transition.effects.isEmpty())

        val readyWhileIdle = reduce(
            CallState.Idle,
            CallEvent.SessionStateChanged(ConnectionState.Ready),
        )
        assertEquals(CallState.Idle, readyWhileIdle.state)
    }

    @Test
    fun describeAndLiveFlagsAreStable() {
        assertTrue(CallState.Starting(StartPhase.Connecting).isLive)
        assertTrue(CallState.Active.isLive)
        assertTrue(CallState.Reconnecting(1, "retry").isLive)
        assertTrue(CallState.Ending.isLive)
        assertFalse(CallState.Idle.isLive)
        assertFalse(CallState.AwaitingMicPermission.isLive)
        assertFalse(CallState.Ended(1).isLive)
        assertFalse(CallState.Failed(CallFailureReason.Unexpected, "x").isLive)
        assertEquals("Active", CallState.Active.describe())
        assertEquals("Reconnecting (attempt 2)", CallState.Reconnecting(2, "x").describe())
    }

    @Test
    fun connectingConnectionStateIsIgnored() {
        val transition = reduce(
            CallState.Starting(StartPhase.ServiceStart),
            CallEvent.SessionStateChanged(
                ConnectionState.Connecting(ConnectingPhase.Probing),
            ),
        )
        assertEquals(CallState.Starting(StartPhase.ServiceStart), transition.state)
        assertTrue(transition.effects.isEmpty())
    }
}
