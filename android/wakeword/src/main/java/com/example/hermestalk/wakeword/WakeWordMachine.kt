package com.example.hermestalk.wakeword

/** Events that drive [WakeWordMachine]. */
sealed interface WakeWordEvent {
    data object StartRequested : WakeWordEvent
    data object EngineReady : WakeWordEvent
    data object CaptureStarted : WakeWordEvent
    data object KeywordDetected : WakeWordEvent
    data object StopRequested : WakeWordEvent
    data object Stopped : WakeWordEvent
    data object CooldownElapsed : WakeWordEvent
    data class CaptureFailed(val reason: String) : WakeWordEvent
    data class EngineUnavailable(val reason: String) : WakeWordEvent
    data object PermissionDenied : WakeWordEvent
}

/** Side effects the caller may perform after a transition. */
sealed interface WakeWordEffect {
    data object CreateEngine : WakeWordEffect
    data object StartCapture : WakeWordEffect
    data object StopCapture : WakeWordEffect
    data object ReleaseEngine : WakeWordEffect
    data object NotifyDetected : WakeWordEffect
    data object ScheduleCooldown : WakeWordEffect
    data object CancelCooldown : WakeWordEffect
}

/** The result of reducing an event against a state. */
data class WakeWordTransition(
    val state: WakeWordState,
    val effects: List<WakeWordEffect> = emptyList(),
)

/**
 * Deterministic reducer for the wake-word lifecycle. Illegal or irrelevant
 * combinations return the current state unchanged. Detected can return to
 * Listening only through an explicit [WakeWordEvent.CooldownElapsed].
 */
object WakeWordMachine {

    fun reduce(current: WakeWordState, event: WakeWordEvent): WakeWordTransition =
        when (event) {
            WakeWordEvent.StartRequested -> when (current) {
                is WakeWordState.Off,
                is WakeWordState.Unavailable,
                is WakeWordState.PermissionDenied,
                is WakeWordState.Failed,
                -> WakeWordTransition(WakeWordState.Starting, listOf(WakeWordEffect.CreateEngine))

                else -> WakeWordTransition(current)
            }

            WakeWordEvent.EngineReady -> when (current) {
                is WakeWordState.Starting ->
                    WakeWordTransition(WakeWordState.Starting, listOf(WakeWordEffect.StartCapture))

                else -> WakeWordTransition(current)
            }

            WakeWordEvent.CaptureStarted -> when (current) {
                is WakeWordState.Starting -> WakeWordTransition(WakeWordState.Listening)
                else -> WakeWordTransition(current)
            }

            WakeWordEvent.KeywordDetected -> when (current) {
                is WakeWordState.Listening -> WakeWordTransition(
                    WakeWordState.Detected,
                    listOf(WakeWordEffect.NotifyDetected, WakeWordEffect.ScheduleCooldown),
                )

                else -> WakeWordTransition(current)
            }

            WakeWordEvent.CooldownElapsed -> when (current) {
                is WakeWordState.Detected -> WakeWordTransition(WakeWordState.Listening)
                else -> WakeWordTransition(current)
            }

            WakeWordEvent.StopRequested -> when (current) {
                is WakeWordState.Off -> WakeWordTransition(WakeWordState.Off)
                is WakeWordState.Stopping -> WakeWordTransition(WakeWordState.Stopping)
                else -> WakeWordTransition(WakeWordState.Stopping, listOf(WakeWordEffect.StopCapture))
            }

            WakeWordEvent.Stopped -> when (current) {
                is WakeWordState.Stopping -> WakeWordTransition(
                    WakeWordState.Off,
                    listOf(WakeWordEffect.ReleaseEngine, WakeWordEffect.CancelCooldown),
                )

                is WakeWordState.Off -> WakeWordTransition(WakeWordState.Off)
                else -> WakeWordTransition(current)
            }

            is WakeWordEvent.CaptureFailed -> when (current) {
                is WakeWordState.Starting,
                is WakeWordState.Listening,
                is WakeWordState.Detected,
                -> WakeWordTransition(
                    WakeWordState.Failed(event.reason),
                    listOf(WakeWordEffect.StopCapture, WakeWordEffect.CancelCooldown),
                )

                else -> WakeWordTransition(current)
            }

            is WakeWordEvent.EngineUnavailable -> when (current) {
                is WakeWordState.Off -> WakeWordTransition(WakeWordState.Unavailable(event.reason))
                else -> WakeWordTransition(
                    WakeWordState.Unavailable(event.reason),
                    listOf(WakeWordEffect.StopCapture, WakeWordEffect.CancelCooldown),
                )
            }

            WakeWordEvent.PermissionDenied -> when (current) {
                is WakeWordState.PermissionDenied -> WakeWordTransition(WakeWordState.PermissionDenied)
                else -> WakeWordTransition(
                    WakeWordState.PermissionDenied,
                    listOf(WakeWordEffect.StopCapture, WakeWordEffect.CancelCooldown),
                )
            }
        }
}
