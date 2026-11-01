package com.example.hermestalk.rtvi

import ai.pipecat.client.PipecatEventCallbacks
import ai.pipecat.client.types.BotReadyData
import ai.pipecat.client.types.TransportState

/**
 * Translates Pipecat transport callbacks into [ConnectionEvent]s and forwards
 * non-state callbacks (transcripts, audio levels) to [callbacks].
 *
 * @param dispatch delivers an event to the session reducer on the main thread.
 */
internal class PipecatEventBridge(
    private val dispatch: (ConnectionEvent) -> Unit,
    private val callbacks: SessionCallbacks,
) : PipecatEventCallbacks() {

    override fun onBackendError(message: String) {
        dispatch(ConnectionEvent.BackendError(message.take(MAX_DETAIL)))
    }

    override fun onConnected() {
        dispatch(ConnectionEvent.TransportConnected)
    }

    override fun onDisconnected() {
        dispatch(ConnectionEvent.TransportDisconnected)
    }

    override fun onTransportStateChanged(state: TransportState) {
        when (state) {
            TransportState.Connected -> dispatch(ConnectionEvent.TransportConnected)
            TransportState.Ready -> dispatch(ConnectionEvent.BotReady)
            TransportState.Disconnected -> dispatch(ConnectionEvent.TransportDisconnected)
            TransportState.Error ->
                dispatch(
                    ConnectionEvent.TransportFailed(
                        FailureReason.Unexpected,
                        "The transport reported an error",
                    ),
                )

            else -> Unit
        }
    }

    override fun onBotReady(data: BotReadyData) {
        dispatch(ConnectionEvent.BotReady)
    }

    override fun onBotTranscript(text: String) {
        callbacks.onBotTranscript(text)
    }

    override fun onUserAudioLevel(level: Float) {
        callbacks.onUserAudioLevel(level)
    }
}
