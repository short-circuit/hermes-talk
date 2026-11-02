package com.example.hermestalk.wakeword

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class WakeWordMachineTest {

    private fun reduce(state: WakeWordState, event: WakeWordEvent): WakeWordTransition =
        WakeWordMachine.reduce(state, event)

    private fun assertUnchanged(state: WakeWordState, event: WakeWordEvent) {
        val transition = reduce(state, event)
        assertEquals(state, transition.state)
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun startFromRestingAndTerminalStatesCreatesTheEngine() {
        val states = listOf(
            WakeWordState.Off,
            WakeWordState.Unavailable("missing model"),
            WakeWordState.PermissionDenied,
            WakeWordState.Failed("capture failed"),
        )
        for (state in states) {
            val transition = reduce(state, WakeWordEvent.StartRequested)
            assertEquals(WakeWordState.Starting, transition.state)
            assertEquals(listOf(WakeWordEffect.CreateEngine), transition.effects)
        }
    }

    @Test
    fun startWhileActiveOrStoppingIsIgnored() {
        assertUnchanged(WakeWordState.Starting, WakeWordEvent.StartRequested)
        assertUnchanged(WakeWordState.Listening, WakeWordEvent.StartRequested)
        assertUnchanged(WakeWordState.Detected, WakeWordEvent.StartRequested)
        assertUnchanged(WakeWordState.Stopping, WakeWordEvent.StartRequested)
    }

    @Test
    fun engineReadyRequestsCapture() {
        val transition = reduce(WakeWordState.Starting, WakeWordEvent.EngineReady)
        assertEquals(WakeWordState.Starting, transition.state)
        assertEquals(listOf(WakeWordEffect.StartCapture), transition.effects)
    }

    @Test
    fun engineReadyOutsideStartingIsIgnored() {
        assertUnchanged(WakeWordState.Off, WakeWordEvent.EngineReady)
        assertUnchanged(WakeWordState.Listening, WakeWordEvent.EngineReady)
        assertUnchanged(WakeWordState.Stopping, WakeWordEvent.EngineReady)
    }

    @Test
    fun captureStartedMovesToListening() {
        val transition = reduce(WakeWordState.Starting, WakeWordEvent.CaptureStarted)
        assertEquals(WakeWordState.Listening, transition.state)
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun captureStartedOutsideStartingIsIgnored() {
        assertUnchanged(WakeWordState.Off, WakeWordEvent.CaptureStarted)
        assertUnchanged(WakeWordState.Listening, WakeWordEvent.CaptureStarted)
        assertUnchanged(WakeWordState.Detected, WakeWordEvent.CaptureStarted)
    }

    @Test
    fun keywordDetectedEntersDetectedWithEffects() {
        val transition = reduce(WakeWordState.Listening, WakeWordEvent.KeywordDetected)
        assertEquals(WakeWordState.Detected, transition.state)
        assertEquals(
            listOf(WakeWordEffect.NotifyDetected, WakeWordEffect.ScheduleCooldown),
            transition.effects,
        )
    }

    @Test
    fun duplicateKeywordWhileDetectedIsIgnored() {
        assertUnchanged(WakeWordState.Detected, WakeWordEvent.KeywordDetected)
        assertUnchanged(WakeWordState.Starting, WakeWordEvent.KeywordDetected)
        assertUnchanged(WakeWordState.Stopping, WakeWordEvent.KeywordDetected)
    }

    @Test
    fun onlyCooldownElapsedReturnsDetectedToListening() {
        val transition = reduce(WakeWordState.Detected, WakeWordEvent.CooldownElapsed)
        assertEquals(WakeWordState.Listening, transition.state)
        assertTrue(transition.effects.isEmpty())
        assertUnchanged(WakeWordState.Listening, WakeWordEvent.CooldownElapsed)
        assertUnchanged(WakeWordState.Starting, WakeWordEvent.CooldownElapsed)
    }

    @Test
    fun stopFromActiveStatesRequestsCaptureStop() {
        val states = listOf(
            WakeWordState.Starting,
            WakeWordState.Listening,
            WakeWordState.Detected,
            WakeWordState.Unavailable("missing model"),
            WakeWordState.PermissionDenied,
            WakeWordState.Failed("capture failed"),
        )
        for (state in states) {
            val transition = reduce(state, WakeWordEvent.StopRequested)
            assertEquals(WakeWordState.Stopping, transition.state)
            assertEquals(listOf(WakeWordEffect.StopCapture), transition.effects)
        }
    }

    @Test
    fun stopFromOffAndStoppingIsIdempotent() {
        assertUnchanged(WakeWordState.Off, WakeWordEvent.StopRequested)
        assertUnchanged(WakeWordState.Stopping, WakeWordEvent.StopRequested)
    }

    @Test
    fun stoppedFromStoppingReleasesResources() {
        val transition = reduce(WakeWordState.Stopping, WakeWordEvent.Stopped)
        assertEquals(WakeWordState.Off, transition.state)
        assertEquals(
            listOf(WakeWordEffect.ReleaseEngine, WakeWordEffect.CancelCooldown),
            transition.effects,
        )
    }

    @Test
    fun stoppedOutsideStoppingIsIgnoredExceptOff() {
        assertUnchanged(WakeWordState.Off, WakeWordEvent.Stopped)
        assertUnchanged(WakeWordState.Listening, WakeWordEvent.Stopped)
        assertUnchanged(WakeWordState.Detected, WakeWordEvent.Stopped)
        assertUnchanged(WakeWordState.Starting, WakeWordEvent.Stopped)
    }

    @Test
    fun captureFailureFailsOnlyActiveStates() {
        val reason = "AudioRecord read failed"
        for (state in listOf(WakeWordState.Starting, WakeWordState.Listening, WakeWordState.Detected)) {
            val transition = reduce(state, WakeWordEvent.CaptureFailed(reason))
            assertEquals(WakeWordState.Failed(reason), transition.state)
            assertEquals(
                listOf(WakeWordEffect.StopCapture, WakeWordEffect.CancelCooldown),
                transition.effects,
            )
        }
        assertUnchanged(WakeWordState.Off, WakeWordEvent.CaptureFailed(reason))
        assertUnchanged(WakeWordState.Stopping, WakeWordEvent.CaptureFailed(reason))
        assertUnchanged(
            WakeWordState.Unavailable("missing model"),
            WakeWordEvent.CaptureFailed(reason),
        )
        assertUnchanged(WakeWordState.PermissionDenied, WakeWordEvent.CaptureFailed(reason))
        assertUnchanged(WakeWordState.Failed("old"), WakeWordEvent.CaptureFailed(reason))
    }

    @Test
    fun engineUnavailableMarksUnavailableAndStopsCapture() {
        val fromOff = reduce(WakeWordState.Off, WakeWordEvent.EngineUnavailable("no native lib"))
        assertEquals(WakeWordState.Unavailable("no native lib"), fromOff.state)
        assertTrue(fromOff.effects.isEmpty())

        for (state in listOf(
            WakeWordState.Starting,
            WakeWordState.Listening,
            WakeWordState.Detected,
            WakeWordState.Stopping,
            WakeWordState.PermissionDenied,
            WakeWordState.Failed("old"),
            WakeWordState.Unavailable("old"),
        )) {
            val transition = reduce(state, WakeWordEvent.EngineUnavailable("no native lib"))
            assertEquals(WakeWordState.Unavailable("no native lib"), transition.state)
            assertEquals(
                listOf(WakeWordEffect.StopCapture, WakeWordEffect.CancelCooldown),
                transition.effects,
            )
        }
    }

    @Test
    fun permissionDeniedMarksDeniedAndStopsCapture() {
        val fromDenied = reduce(WakeWordState.PermissionDenied, WakeWordEvent.PermissionDenied)
        assertEquals(WakeWordState.PermissionDenied, fromDenied.state)
        assertTrue(fromDenied.effects.isEmpty())

        for (state in listOf(
            WakeWordState.Off,
            WakeWordState.Starting,
            WakeWordState.Listening,
            WakeWordState.Detected,
            WakeWordState.Stopping,
            WakeWordState.Unavailable("old"),
            WakeWordState.Failed("old"),
        )) {
            val transition = reduce(state, WakeWordEvent.PermissionDenied)
            assertEquals(WakeWordState.PermissionDenied, transition.state)
            assertEquals(
                listOf(WakeWordEffect.StopCapture, WakeWordEffect.CancelCooldown),
                transition.effects,
            )
        }
    }

    @Test
    fun activeAndTerminalStateDescriptionsAreStable() {
        assertEquals("Off", WakeWordState.Off.describe())
        assertEquals("Listening", WakeWordState.Listening.describe())
        assertEquals("Unavailable: no model", WakeWordState.Unavailable("no model").describe())
        assertEquals("Failed: boom", WakeWordState.Failed("boom").describe())
        assertTrue(WakeWordState.Listening.isActive)
        assertTrue(WakeWordState.Detected.isActive)
        assertTrue(!WakeWordState.Stopping.isActive)
        assertTrue(!WakeWordState.PermissionDenied.isActive)
    }
}
