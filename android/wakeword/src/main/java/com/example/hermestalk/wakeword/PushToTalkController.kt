package com.example.hermestalk.wakeword

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/** Push-to-talk states. */
sealed interface PushToTalkState {
    data object Idle : PushToTalkState
    data object Pressed : PushToTalkState
    data object Released : PushToTalkState
}

/** Events that drive the push-to-talk machine. */
sealed interface PushToTalkEvent {
    data object PressStarted : PushToTalkEvent
    data object PressEnded : PushToTalkEvent
    data class CaptureFailed(val reason: String) : PushToTalkEvent
    data object PermissionDenied : PushToTalkEvent
}

/** Effects the caller must perform. */
sealed interface PushToTalkEffect {
    data object StartSessionRequested : PushToTalkEffect
}

/** The result of reducing a push-to-talk event. */
data class PushToTalkTransition(
    val state: PushToTalkState,
    val effects: List<PushToTalkEffect> = emptyList(),
)

/**
 * Deterministic push-to-talk reducer. It never depends on the keyword engine,
 * so the fallback works when the engine is unavailable.
 */
object PushToTalkMachine {

    fun reduce(current: PushToTalkState, event: PushToTalkEvent): PushToTalkTransition =
        when (event) {
            PushToTalkEvent.PressStarted -> when (current) {
                is PushToTalkState.Idle,
                is PushToTalkState.Released,
                -> PushToTalkTransition(PushToTalkState.Pressed)

                is PushToTalkState.Pressed -> PushToTalkTransition(PushToTalkState.Pressed)
            }

            PushToTalkEvent.PressEnded -> when (current) {
                is PushToTalkState.Pressed -> PushToTalkTransition(
                    PushToTalkState.Released,
                    listOf(PushToTalkEffect.StartSessionRequested),
                )

                is PushToTalkState.Idle -> PushToTalkTransition(PushToTalkState.Idle)
                is PushToTalkState.Released -> PushToTalkTransition(PushToTalkState.Released)
            }

            is PushToTalkEvent.CaptureFailed -> when (current) {
                is PushToTalkState.Pressed -> PushToTalkTransition(PushToTalkState.Idle)
                else -> PushToTalkTransition(current)
            }

            PushToTalkEvent.PermissionDenied -> PushToTalkTransition(PushToTalkState.Idle)
        }
}

/** Small StateFlow wrapper around [PushToTalkMachine]. */
class PushToTalkController {

    private val _state = MutableStateFlow<PushToTalkState>(PushToTalkState.Idle)

    /** Observable push-to-talk state. */
    val state: StateFlow<PushToTalkState> = _state.asStateFlow()

    fun pressStarted(): PushToTalkTransition = apply(PushToTalkEvent.PressStarted)

    fun pressEnded(): PushToTalkTransition = apply(PushToTalkEvent.PressEnded)

    fun captureFailed(reason: String): PushToTalkTransition =
        apply(PushToTalkEvent.CaptureFailed(reason))

    fun permissionDenied(): PushToTalkTransition = apply(PushToTalkEvent.PermissionDenied)

    /** Reset to Idle without emitting an effect. */
    fun reset() {
        _state.value = PushToTalkState.Idle
    }

    private fun apply(event: PushToTalkEvent): PushToTalkTransition {
        val transition = PushToTalkMachine.reduce(_state.value, event)
        _state.value = transition.state
        return transition
    }
}
