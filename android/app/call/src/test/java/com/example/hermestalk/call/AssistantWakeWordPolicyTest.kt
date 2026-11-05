package com.example.hermestalk.call

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AssistantWakeWordPolicyTest {

    private fun inputs(
        wakeWordEnabled: Boolean = true,
        assistantEnabled: Boolean = true,
        callLive: Boolean = false,
        hasRecordAudio: Boolean = true,
        foregroundType: AssistantForegroundType = AssistantForegroundType.Microphone,
    ) = AssistantListeningInputs(
        wakeWordEnabled = wakeWordEnabled,
        assistantEnabled = assistantEnabled,
        callLive = callLive,
        hasRecordAudio = hasRecordAudio,
        foregroundType = foregroundType,
    )

    @Test
    fun callWinsOverWakeWordEvenWhenEverythingElseAllowsIt() {
        assertEquals(
            AssistantMicOwner.Call,
            AssistantWakeWordPolicy.micOwner(inputs(callLive = true)),
        )
    }

    @Test
    fun wakeWordRequiresTheMicrophoneForegroundType() {
        assertEquals(
            AssistantMicOwner.None,
            AssistantWakeWordPolicy.micOwner(
                inputs(foregroundType = AssistantForegroundType.SpecialUse),
            ),
        )
        assertEquals(
            AssistantMicOwner.None,
            AssistantWakeWordPolicy.micOwner(
                inputs(foregroundType = AssistantForegroundType.None),
            ),
        )
        assertEquals(
            AssistantMicOwner.WakeWord,
            AssistantWakeWordPolicy.micOwner(
                inputs(foregroundType = AssistantForegroundType.Microphone),
            ),
        )
    }

    @Test
    fun wakeWordRequiresRecordAudio() {
        assertEquals(
            AssistantMicOwner.None,
            AssistantWakeWordPolicy.micOwner(inputs(hasRecordAudio = false)),
        )
    }

    @Test
    fun wakeWordRequiresTheAssistantToBeEnabled() {
        assertEquals(
            AssistantMicOwner.None,
            AssistantWakeWordPolicy.micOwner(inputs(assistantEnabled = false)),
        )
    }

    @Test
    fun ownerIsNoneOtherwise() {
        assertEquals(
            AssistantMicOwner.None,
            AssistantWakeWordPolicy.micOwner(inputs(wakeWordEnabled = false)),
        )
    }

    @Test
    fun shouldCaptureMatchesWakeWordOwnership() {
        val cases = listOf(
            inputs(),
            inputs(callLive = true),
            inputs(wakeWordEnabled = false),
            inputs(assistantEnabled = false),
            inputs(hasRecordAudio = false),
            inputs(foregroundType = AssistantForegroundType.SpecialUse),
            inputs(foregroundType = AssistantForegroundType.None),
        )
        for (case in cases) {
            assertEquals(
                AssistantWakeWordPolicy.micOwner(case) == AssistantMicOwner.WakeWord,
                AssistantWakeWordPolicy.shouldCapture(case),
            )
        }
    }

    @Test
    fun detectionStepsAreInTheRequiredOrder() {
        assertEquals(
            listOf(
                AssistantDetectionStep.StopCapture,
                AssistantDetectionStep.ReleaseListeningWakeLock,
                AssistantDetectionStep.AbandonAudioFocus,
                AssistantDetectionStep.ClearCommunicationDevice,
                AssistantDetectionStep.ExitCommunicationMode,
                AssistantDetectionStep.LaunchAssistant,
            ),
            AssistantWakeWordPolicy.detectionSteps(),
        )
    }

    @Test
    fun detectionBudgetBoundaries() {
        assertTrue(AssistantWakeWordPolicy.isWithinBudget(0L))
        assertTrue(AssistantWakeWordPolicy.isWithinBudget(999L))
        assertTrue(AssistantWakeWordPolicy.isWithinBudget(1000L))
        assertFalse(AssistantWakeWordPolicy.isWithinBudget(1001L))
        assertFalse(AssistantWakeWordPolicy.isWithinBudget(-1L))
    }

    @Test
    fun resumeWaitsForTheRestartDelay() {
        val inputs = inputs()
        assertFalse(
            AssistantWakeWordPolicy.shouldResumeAfterDetection(
                nowMillis = 1_000L,
                detectedAtMillis = 1_000L,
                restartDelayMillis = 500L,
                inputs = inputs,
            ),
        )
        assertTrue(
            AssistantWakeWordPolicy.shouldResumeAfterDetection(
                nowMillis = 1_500L,
                detectedAtMillis = 1_000L,
                restartDelayMillis = 500L,
                inputs = inputs,
            ),
        )
    }

    @Test
    fun resumeIsBlockedWhileACallIsLive() {
        assertFalse(
            AssistantWakeWordPolicy.shouldResumeAfterDetection(
                nowMillis = 10_000L,
                detectedAtMillis = 1_000L,
                restartDelayMillis = 500L,
                inputs = inputs(callLive = true),
            ),
        )
    }
}
