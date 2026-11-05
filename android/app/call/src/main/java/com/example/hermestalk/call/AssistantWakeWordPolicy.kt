package com.example.hermestalk.call

/** Which component currently owns the microphone. */
enum class AssistantMicOwner {
    /** No component is capturing. */
    None,

    /** The persistent assistant is running on-device wake-word capture. */
    WakeWord,

    /** A live call owns the microphone. */
    Call,
}

/** Inputs to the microphone-ownership policy. */
data class AssistantListeningInputs(
    val wakeWordEnabled: Boolean,
    val assistantEnabled: Boolean,
    val callLive: Boolean,
    val hasRecordAudio: Boolean,
    val foregroundType: AssistantForegroundType,
)

/** Ordered steps performed when the wake word fires. */
enum class AssistantDetectionStep {
    StopCapture,
    ReleaseListeningWakeLock,
    AbandonAudioFocus,
    ClearCommunicationDevice,
    ExitCommunicationMode,
    LaunchAssistant,
}

/**
 * Pure microphone-ownership and detection policy for the always-on assistant.
 *
 * The policy has no Android dependencies so the arbitration between a live call
 * and wake-word capture can be exercised by JVM unit tests. A live call always
 * wins; wake-word capture requires the microphone foreground type because
 * Android 14 denies background capture without it.
 */
object AssistantWakeWordPolicy {

    /** Maximum measured time from detection to the call screen opening. */
    const val DETECTION_TO_CALL_BUDGET_MILLIS = 1000L

    /** The component that owns the microphone for [inputs]. */
    fun micOwner(inputs: AssistantListeningInputs): AssistantMicOwner {
        if (inputs.callLive) {
            return AssistantMicOwner.Call
        }
        if (
            inputs.wakeWordEnabled &&
            inputs.assistantEnabled &&
            inputs.hasRecordAudio &&
            inputs.foregroundType == AssistantForegroundType.Microphone
        ) {
            return AssistantMicOwner.WakeWord
        }
        return AssistantMicOwner.None
    }

    /** True when the wake-word runner should hold the microphone. */
    fun shouldCapture(inputs: AssistantListeningInputs): Boolean =
        micOwner(inputs) == AssistantMicOwner.WakeWord

    /** The exact ordered cleanup and launch sequence for a detection. */
    fun detectionSteps(): List<AssistantDetectionStep> = listOf(
        AssistantDetectionStep.StopCapture,
        AssistantDetectionStep.ReleaseListeningWakeLock,
        AssistantDetectionStep.AbandonAudioFocus,
        AssistantDetectionStep.ClearCommunicationDevice,
        AssistantDetectionStep.ExitCommunicationMode,
        AssistantDetectionStep.LaunchAssistant,
    )

    /** True when [elapsedMillis] is inside the detection-to-call budget. */
    fun isWithinBudget(elapsedMillis: Long): Boolean =
        elapsedMillis in 0L..DETECTION_TO_CALL_BUDGET_MILLIS

    /**
     * True when capture may resume after a detection: the restart delay has
     * elapsed, no call is live and the policy still grants the microphone to
     * the wake word.
     */
    fun shouldResumeAfterDetection(
        nowMillis: Long,
        detectedAtMillis: Long,
        restartDelayMillis: Long,
        inputs: AssistantListeningInputs,
    ): Boolean {
        if (inputs.callLive) {
            return false
        }
        val elapsed = nowMillis - detectedAtMillis
        if (elapsed < restartDelayMillis) {
            return false
        }
        return shouldCapture(inputs)
    }
}
