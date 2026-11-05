package com.example.hermestalk.call

/**
 * Microphone-coordination seam between the call controller and the persistent
 * wake-word runner. A live call must deterministically win the microphone.
 */
interface AssistantMicCoordinator {
    /** Stop wake-word capture before the call service starts. */
    fun suspendWakeWord(): Boolean

    /** Allow wake-word capture to resume after the call releases the microphone. */
    fun resumeWakeWord()
}

/** No-op coordinator used by call-controller unit tests. */
object NoAssistantMicCoordinator : AssistantMicCoordinator {
    override fun suspendWakeWord(): Boolean = true

    override fun resumeWakeWord() = Unit
}

/** Production coordinator backed by the process-wide wake-word runner. */
class ProcessAssistantMicCoordinator : AssistantMicCoordinator {
    override fun suspendWakeWord(): Boolean = AssistantWakeWord.suspendForCall()

    override fun resumeWakeWord() {
        AssistantWakeWord.resumeAfterCall()
    }
}
