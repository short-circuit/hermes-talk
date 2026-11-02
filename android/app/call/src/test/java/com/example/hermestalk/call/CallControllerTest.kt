package com.example.hermestalk.call

import android.content.ContextWrapper
import android.content.Intent
import android.content.pm.PackageManager
import com.example.hermestalk.rtvi.ConnectionState
import com.example.hermestalk.rtvi.ConnectingPhase
import com.example.hermestalk.rtvi.FailureReason
import com.example.hermestalk.rtvi.RelaySettings
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CallControllerTest {

    private class FakeContext(private val granted: Boolean) : ContextWrapper(null) {
        override fun checkPermission(permission: String, pid: Int, uid: Int): Int =
            if (granted) {
                PackageManager.PERMISSION_GRANTED
            } else {
                PackageManager.PERMISSION_DENIED
            }
    }

    private class FakeSession(
        private val callbacks: CallSessionCallbacks,
        private val failOnConnect: Boolean = false,
    ) : CallSession {
        var connectCalls = 0
        var stopCalls = 0
        var lastMicEnabled: Boolean? = null

        override suspend fun connect(settings: RelaySettings) {
            connectCalls += 1
            callbacks.onConnectionState(ConnectionState.Connecting(ConnectingPhase.Probing))
            callbacks.onConnectionState(ConnectionState.Connected)
            if (failOnConnect) {
                callbacks.onConnectionState(
                    ConnectionState.Failed(
                        FailureReason.Unreachable,
                        "offline",
                        retryable = true,
                    ),
                )
            } else {
                callbacks.onConnectionState(ConnectionState.Ready)
            }
        }

        override fun setMicEnabled(enabled: Boolean) {
            lastMicEnabled = enabled
        }

        override fun stop() {
            stopCalls += 1
        }

        override val connected: Boolean
            get() = true
    }

    private class FakeAudioFocus : AudioFocusController {
        private val state = MutableStateFlow(AudioFocusOutcome.Denied)
        var requests = 0
        var abandons = 0
        var nextOutcome = AudioFocusOutcome.Gained

        override val outcome: StateFlow<AudioFocusOutcome> = state.asStateFlow()

        override fun request(): AudioFocusOutcome {
            requests += 1
            state.value = nextOutcome
            return nextOutcome
        }

        override fun abandon() {
            abandons += 1
            state.value = AudioFocusOutcome.Denied
        }

        fun emit(outcome: AudioFocusOutcome) {
            state.value = outcome
        }
    }

    private class FakeRoutes : AudioRouteController {
        var available = listOf(AudioRoute.Earpiece, AudioRoute.Speaker)
        var current = AudioRoute.Earpiece
        val selections = mutableListOf<AudioRoute>()
        var enterCalls = 0
        var exitCalls = 0
        var clearCalls = 0
        var monitoring = false
        var onChanged: ((List<AudioRoute>) -> Unit)? = null

        override fun availableRoutes(): List<AudioRoute> = available

        override fun currentRoute(): AudioRoute = current

        override fun select(route: AudioRoute): Boolean {
            selections += route
            if (route !in available) {
                return false
            }
            current = route
            return true
        }

        override fun startMonitoring(onChanged: (List<AudioRoute>) -> Unit) {
            monitoring = true
            this.onChanged = onChanged
        }

        override fun stopMonitoring() {
            monitoring = false
            onChanged = null
        }

        override fun enterCommunicationMode() {
            enterCalls += 1
        }

        override fun exitCommunicationMode() {
            exitCalls += 1
        }

        override fun clearDevice() {
            clearCalls += 1
            current = AudioRoute.Earpiece
        }
    }

    private class FakeMicMonitor : MicActivityMonitor {
        var recording = false
        var started = false
        var stopped = false
        var onChanged: ((Boolean) -> Unit)? = null

        override fun start(onChanged: (Boolean) -> Unit) {
            started = true
            this.onChanged = onChanged
            onChanged(recording)
        }

        override fun stop() {
            stopped = true
            onChanged = null
            recording = false
        }

        override fun isRecording(): Boolean = recording
    }

    private class FakeForeground : CallForegroundController {
        var starts = 0
        var updates = 0
        var stops = 0
        var lastControls: CallControls? = null

        override fun start() {
            starts += 1
        }

        override fun update(controls: CallControls) {
            updates += 1
            lastControls = controls
        }

        override fun stop() {
            stops += 1
        }
    }

    private class FakeBattery(private val ignoring: Boolean = false) : BatteryOptimisation {
        override fun isIgnoring(): Boolean = ignoring
        override fun requestIntent(): Intent = Intent()
    }

    private class Harness(
        granted: Boolean,
        failOnConnect: Boolean,
        val dispatcher: TestDispatcher,
    ) {
        val focus = FakeAudioFocus()
        val routes = FakeRoutes()
        val mic = FakeMicMonitor()
        val foreground = FakeForeground()
        val battery = FakeBattery()
        var session: FakeSession? = null

        val scope = CoroutineScope(SupervisorJob() + dispatcher)
        val controller = CallController(
            appContext = FakeContext(granted),
            sessionFactory = { callbacks ->
                FakeSession(callbacks, failOnConnect).also { session = it }
            },
            audioFocus = focus,
            routes = routes,
            micMonitor = mic,
            foreground = foreground,
            battery = battery,
            scope = scope,
            clock = { 1_000L },
        )
    }

    private val settings = RelaySettings("http://127.0.0.1:8790", "device-token")

    @Test
    fun startPermissionActive() = runTest {
        val harness = Harness(
            granted = false,
            failOnConnect = false,
            dispatcher = StandardTestDispatcher(testScheduler),
        )
        harness.controller.startCall(settings)
        assertEquals(CallState.AwaitingMicPermission, harness.controller.uiState.value.state)
        assertEquals(0, harness.foreground.starts)

        harness.controller.onMicPermissionResult(true)
        testScheduler.runCurrent()

        assertEquals(CallState.Active, harness.controller.uiState.value.state)
        assertEquals(1, harness.foreground.starts)
        assertEquals(1, harness.focus.requests)
        assertNotNull(harness.session)
        assertEquals(1, harness.session?.connectCalls)
        assertEquals(true, harness.session?.lastMicEnabled)

        harness.controller.release()
    }

    @Test
    fun startWithGrantedPermissionConnectsImmediately() = runTest {
        val harness = Harness(granted = true, failOnConnect = false, StandardTestDispatcher(testScheduler))
        harness.controller.startCall(settings)
        testScheduler.runCurrent()

        assertEquals(CallState.Active, harness.controller.uiState.value.state)
        assertEquals(1, harness.foreground.starts)
        assertEquals(1, harness.session?.connectCalls)

        harness.controller.release()
    }

    @Test
    fun muteCallsSetMicEnabledFalseWithoutChangingState() = runTest {
        val harness = activeHarness()
        harness.controller.toggleMute()
        testScheduler.runCurrent()

        assertEquals(CallState.Active, harness.controller.uiState.value.state)
        assertTrue(harness.controller.uiState.value.controls.muted)
        assertEquals(false, harness.session?.lastMicEnabled)
        assertTrue(harness.foreground.updates >= 1)
        assertEquals(0, harness.session?.stopCalls)

        harness.controller.release()
    }

    @Test
    fun focusLossMutesAndDoesNotStopSession() = runTest {
        val harness = activeHarness()
        harness.focus.emit(AudioFocusOutcome.Lost)
        testScheduler.runCurrent()

        assertEquals(CallState.Active, harness.controller.uiState.value.state)
        assertTrue(harness.controller.uiState.value.controls.muted)
        assertFalse(harness.controller.uiState.value.controls.audioFocusHeld)
        assertEquals(false, harness.session?.lastMicEnabled)
        assertEquals(0, harness.session?.stopCalls)

        harness.controller.release()
    }

    @Test
    fun endCallStopsSessionOnceAndReleasesAudio() = runTest {
        val harness = activeHarness()
        harness.controller.endCall()

        assertEquals(CallState.Ended(0L), harness.controller.uiState.value.state)
        assertEquals(1, harness.session?.stopCalls)
        assertTrue(harness.foreground.stops >= 1)
        assertTrue(harness.focus.abandons >= 1)
        assertTrue(harness.routes.clearCalls >= 1)
        assertTrue(harness.routes.exitCalls >= 1)
        assertFalse(harness.controller.uiState.value.controls.micActive)
        assertTrue(harness.mic.stopped)

        harness.controller.endCall()
        assertEquals(1, harness.session?.stopCalls)
        assertEquals(1, harness.foreground.starts)

        harness.controller.release()
    }

    @Test
    fun relayFailureCleansUpAndFails() = runTest {
        val harness = Harness(granted = true, failOnConnect = true, StandardTestDispatcher(testScheduler))
        harness.controller.startCall(settings)
        testScheduler.runCurrent()

        val state = harness.controller.uiState.value.state
        assertTrue(state is CallState.Failed)
        assertEquals(CallFailureReason.RelayUnreachable, (state as CallState.Failed).reason)
        assertEquals(1, harness.session?.stopCalls)
        assertTrue(harness.foreground.stops >= 1)
        assertTrue(harness.focus.abandons >= 1)

        harness.controller.release()
    }

    @Test
    fun routeChangeWhileActiveDoesNotStopSession() = runTest {
        val harness = activeHarness()
        harness.routes.available = listOf(
            AudioRoute.Earpiece,
            AudioRoute.Speaker,
            AudioRoute.Bluetooth,
        )
        harness.controller.onAudioDevicesChanged()
        testScheduler.runCurrent()

        assertEquals(CallState.Active, harness.controller.uiState.value.state)
        assertEquals(0, harness.session?.stopCalls)
        assertTrue(harness.routes.selections.isNotEmpty())

        harness.routes.onChanged?.invoke(listOf(AudioRoute.Earpiece, AudioRoute.Speaker))
        testScheduler.runCurrent()
        assertEquals(CallState.Active, harness.controller.uiState.value.state)
        assertEquals(0, harness.session?.stopCalls)

        harness.controller.release()
    }

    @Test
    fun permissionDeniedFailsWithoutStartingService() = runTest {
        val harness = Harness(granted = false, failOnConnect = false, StandardTestDispatcher(testScheduler))
        harness.controller.startCall(settings)
        harness.controller.onMicPermissionResult(false)

        val state = harness.controller.uiState.value.state
        assertTrue(state is CallState.Failed)
        assertEquals(
            CallFailureReason.MicrophonePermissionDenied,
            (state as CallState.Failed).reason,
        )
        assertEquals(0, harness.foreground.starts)
        assertTrue(harness.foreground.stops >= 1)
        assertTrue(harness.focus.abandons >= 1)

        harness.controller.release()
    }

    private fun TestScope.activeHarness(): Harness {
        val harness = Harness(
            granted = true,
            failOnConnect = false,
            dispatcher = StandardTestDispatcher(testScheduler),
        )
        harness.controller.startCall(settings)
        testScheduler.runCurrent()
        assertEquals(CallState.Active, harness.controller.uiState.value.state)
        return harness
    }
}
