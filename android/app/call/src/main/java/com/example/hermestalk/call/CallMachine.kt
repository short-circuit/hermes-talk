package com.example.hermestalk.call

import com.example.hermestalk.rtvi.ConnectionState
import com.example.hermestalk.rtvi.FailureReason

/** Events that drive [CallMachine]. */
sealed interface CallEvent {
    /** The user asked to start a call. */
    data object StartRequested : CallEvent

    /** The microphone runtime permission was granted. */
    data object MicPermissionGranted : CallEvent

    /** The microphone runtime permission was denied. */
    data object MicPermissionDenied : CallEvent

    /** The microphone permission was revoked while a call was running. */
    data object MicPermissionRevoked : CallEvent

    /** The foreground service started successfully. */
    data object ServiceStarted : CallEvent

    /** The RTVI session reported a connection state. */
    data class SessionStateChanged(val connection: ConnectionState) : CallEvent

    /** The RTVI session failed outside its own state machine. */
    data class SessionFailed(val reason: CallFailureReason, val detail: String) : CallEvent

    /** The user asked to mute the microphone. */
    data object MuteRequested : CallEvent

    /** The user asked to unmute the microphone. */
    data object UnmuteRequested : CallEvent

    /** The user selected an audio route. */
    data class RouteSelected(val route: AudioRoute) : CallEvent

    /** The set of available audio devices changed. */
    data class AudioDevicesChanged(val availableRoutes: List<AudioRoute>) : CallEvent

    /** The audio-focus state changed. */
    data class AudioFocusChanged(val outcome: AudioFocusOutcome) : CallEvent

    /** The user asked to end the call. */
    data object EndRequested : CallEvent

    /** Session teardown completed. */
    data object EndCompleted : CallEvent

    /** The one-second call timer fired. */
    data class Tick(val elapsedSeconds: Long) : CallEvent

    /** The user dismissed the ended state. */
    data object AcknowledgeEnded : CallEvent
}

/** Side effects the caller must perform after a transition. */
sealed interface CallEffect {
    /** Ask the UI to request the microphone permission. */
    data object RequestMicPermission : CallEffect

    /** Start the microphone foreground service. */
    data object StartForegroundService : CallEffect

    /** Refresh the foreground notification. */
    data object UpdateForegroundNotification : CallEffect

    /** Stop the foreground service. */
    data object StopForegroundService : CallEffect

    /** Connect the RTVI session. */
    data object ConnectSession : CallEffect

    /** Disconnect and release the RTVI session. */
    data object DisconnectAndReleaseSession : CallEffect

    /** Acquire audio focus for voice communication. */
    data object AcquireAudioFocus : CallEffect

    /** Abandon audio focus. */
    data object AbandonAudioFocus : CallEffect

    /** Apply the effective microphone mute state. */
    data object ApplyMicState : CallEffect

    /** Apply the selected audio route. */
    data object ApplyAudioRoute : CallEffect

    /** Clear the communication device. */
    data object ClearCommunicationDevice : CallEffect

    /** Restore the normal audio mode. */
    data object RestoreAudioMode : CallEffect

    /** Ask the UI to show the battery-optimisation prompt. */
    data object PromptBatteryOptimisation : CallEffect

    /** Start the one-second call timer. */
    data object StartTimer : CallEffect

    /** Stop the one-second call timer. */
    data object StopTimer : CallEffect
}

/** The result of reducing an event against a state. */
data class CallTransition(
    val state: CallState,
    val controls: CallControls,
    val effects: List<CallEffect> = emptyList(),
)

/**
 * Deterministic, pure reducer for the call lifecycle. It has no Android or
 * coroutine dependencies, so the controller, the service and the UI can all
 * reason about transitions without side effects.
 */
object CallMachine {

    private val cleanupEffects = listOf(
        CallEffect.DisconnectAndReleaseSession,
        CallEffect.StopForegroundService,
        CallEffect.AbandonAudioFocus,
        CallEffect.StopTimer,
        CallEffect.ClearCommunicationDevice,
        CallEffect.RestoreAudioMode,
    )

    /**
     * Apply [event] to [current]. Unknown or irrelevant combinations return the
     * current state unchanged and never throw.
     */
    fun reduce(current: CallUiState, event: CallEvent): CallTransition {
        val state = current.state
        val controls = current.controls

        return when (event) {
            CallEvent.StartRequested -> when (state) {
                is CallState.Idle,
                is CallState.Ended,
                is CallState.Failed,
                -> CallTransition(
                    CallState.AwaitingMicPermission,
                    CallControls(),
                    listOf(CallEffect.RequestMicPermission),
                )

                else -> CallTransition(state, controls)
            }

            CallEvent.MicPermissionGranted -> when (state) {
                is CallState.AwaitingMicPermission -> CallTransition(
                    CallState.Starting(StartPhase.ServiceStart),
                    controls.copy(micActive = false, muted = false),
                    listOf(
                        CallEffect.StartForegroundService,
                        CallEffect.AcquireAudioFocus,
                        CallEffect.StartTimer,
                    ),
                )

                else -> CallTransition(state, controls)
            }

            CallEvent.MicPermissionDenied,
            CallEvent.MicPermissionRevoked,
            -> CallTransition(
                CallState.Failed(
                    CallFailureReason.MicrophonePermissionDenied,
                    if (event == CallEvent.MicPermissionRevoked) {
                        "Microphone permission revoked"
                    } else {
                        "Microphone permission denied"
                    },
                ),
                controls.copy(muted = true, micActive = false, audioFocusHeld = false),
                listOf(
                    CallEffect.StopForegroundService,
                    CallEffect.AbandonAudioFocus,
                    CallEffect.StopTimer,
                    CallEffect.DisconnectAndReleaseSession,
                ),
            )

            CallEvent.ServiceStarted -> when (state) {
                is CallState.Starting -> if (state.phase == StartPhase.ServiceStart) {
                    CallTransition(
                        CallState.Starting(StartPhase.Connecting),
                        controls,
                        listOf(CallEffect.ConnectSession),
                    )
                } else {
                    CallTransition(state, controls)
                }

                else -> CallTransition(state, controls)
            }

            is CallEvent.SessionStateChanged -> sessionStateChanged(state, controls, event)

            is CallEvent.SessionFailed -> if (state.isLive) {
                CallTransition(
                    CallState.Failed(event.reason, event.detail),
                    controls.copy(muted = true, micActive = false, audioFocusHeld = false),
                    cleanupEffects,
                )
            } else {
                CallTransition(state, controls)
            }

            CallEvent.MuteRequested -> when (state) {
                is CallState.Active,
                is CallState.Reconnecting,
                -> CallTransition(
                    state,
                    controls.copy(userMuted = true, muted = true),
                    listOf(CallEffect.ApplyMicState, CallEffect.UpdateForegroundNotification),
                )

                else -> CallTransition(state, controls)
            }

            CallEvent.UnmuteRequested -> when (state) {
                is CallState.Active,
                is CallState.Reconnecting,
                -> CallTransition(
                    state,
                    controls.copy(userMuted = false, muted = false),
                    listOf(CallEffect.ApplyMicState, CallEffect.UpdateForegroundNotification),
                )

                else -> CallTransition(state, controls)
            }

            is CallEvent.RouteSelected -> when (state) {
                is CallState.Active,
                is CallState.Starting,
                is CallState.Reconnecting,
                -> if (event.route in controls.availableRoutes) {
                    CallTransition(
                        state,
                        controls.copy(route = event.route),
                        listOf(CallEffect.ApplyAudioRoute),
                    )
                } else {
                    CallTransition(state, controls)
                }

                else -> CallTransition(state, controls)
            }

            is CallEvent.AudioDevicesChanged -> {
                val available = event.availableRoutes.distinct()
                val route = if (controls.route in available) {
                    controls.route
                } else {
                    CallAudioRoutePlanner.fallback(controls.route, available)
                }
                CallTransition(
                    state,
                    controls.copy(availableRoutes = available, route = route),
                    listOf(CallEffect.ApplyAudioRoute),
                )
            }

            is CallEvent.AudioFocusChanged -> audioFocusChanged(state, controls, event.outcome)

            CallEvent.EndRequested -> if (state.isLive && state !is CallState.Ending) {
                CallTransition(
                    CallState.Ending,
                    controls.copy(muted = true, micActive = false, audioFocusHeld = false),
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
                )
            } else {
                CallTransition(state, controls)
            }

            CallEvent.EndCompleted -> when (state) {
                is CallState.Ending -> CallTransition(
                    CallState.Ended(controls.durationSeconds),
                    CallControls(durationSeconds = controls.durationSeconds),
                )

                else -> CallTransition(state, controls)
            }

            is CallEvent.Tick -> when (state) {
                is CallState.Active -> CallTransition(
                    state,
                    controls.copy(durationSeconds = event.elapsedSeconds.coerceAtLeast(0L)),
                )

                else -> CallTransition(state, controls)
            }

            CallEvent.AcknowledgeEnded -> when (state) {
                is CallState.Ended -> CallTransition(CallState.Idle, CallControls())
                else -> CallTransition(state, controls)
            }
        }
    }

    private fun sessionStateChanged(
        state: CallState,
        controls: CallControls,
        event: CallEvent.SessionStateChanged,
    ): CallTransition = when (val connection = event.connection) {
        is ConnectionState.Connecting -> CallTransition(state, controls)

        is ConnectionState.Connected -> when (state) {
            is CallState.Starting -> CallTransition(
                CallState.Starting(StartPhase.Connecting),
                controls,
            )

            else -> CallTransition(state, controls)
        }

        is ConnectionState.Ready -> when (state) {
            is CallState.Starting,
            is CallState.Reconnecting,
            -> {
                val muted = controls.userMuted
                CallTransition(
                    CallState.Active,
                    controls.copy(muted = muted, micActive = !muted),
                    listOf(
                        CallEffect.ApplyMicState,
                        CallEffect.ApplyAudioRoute,
                        CallEffect.UpdateForegroundNotification,
                    ),
                )
            }

            else -> CallTransition(state, controls)
        }

        is ConnectionState.Reconnecting -> if (state.isLive) {
            CallTransition(
                CallState.Reconnecting(connection.attempt, "Relay reconnecting: ${connection.reason}"),
                controls.copy(muted = true, micActive = false),
                listOf(CallEffect.UpdateForegroundNotification),
            )
        } else {
            CallTransition(state, controls)
        }

        is ConnectionState.Disconnected -> if (state.isLive) {
            CallTransition(
                CallState.Failed(CallFailureReason.RelayUnreachable, "Relay connection closed"),
                controls.copy(muted = true, micActive = false, audioFocusHeld = false),
                cleanupEffects,
            )
        } else {
            CallTransition(state, controls)
        }

        is ConnectionState.Failed -> if (state.isLive) {
            CallTransition(
                CallState.Failed(mapFailure(connection.reason), connection.detail),
                controls.copy(muted = true, micActive = false, audioFocusHeld = false),
                cleanupEffects,
            )
        } else {
            CallTransition(state, controls)
        }
    }

    private fun audioFocusChanged(
        state: CallState,
        controls: CallControls,
        outcome: AudioFocusOutcome,
    ): CallTransition = when (outcome) {
        AudioFocusOutcome.Lost -> CallTransition(
            state,
            controls.copy(muted = true, audioFocusHeld = false),
            listOf(CallEffect.ApplyMicState),
        )

        AudioFocusOutcome.LostTransient -> CallTransition(
            state,
            controls.copy(muted = true),
            listOf(CallEffect.ApplyMicState),
        )

        AudioFocusOutcome.Gained -> CallTransition(
            state,
            controls.copy(muted = controls.userMuted, audioFocusHeld = true),
            listOf(CallEffect.ApplyMicState),
        )

        AudioFocusOutcome.Denied -> CallTransition(
            state,
            controls.copy(audioFocusHeld = false),
        )

        AudioFocusOutcome.Duck -> CallTransition(state, controls)
    }

    private fun mapFailure(reason: FailureReason): CallFailureReason = when (reason) {
        FailureReason.Unreachable,
        FailureReason.NotReady,
        FailureReason.TransportClosed,
        -> CallFailureReason.RelayUnreachable

        FailureReason.Unauthorized,
        FailureReason.PermissionDenied,
        FailureReason.BackendError,
        FailureReason.TlsRejected,
        -> CallFailureReason.RelayRejected

        FailureReason.HandshakeFailed -> CallFailureReason.HandshakeFailed

        FailureReason.InvalidConfiguration,
        FailureReason.Unexpected,
        -> CallFailureReason.Unexpected
    }
}
