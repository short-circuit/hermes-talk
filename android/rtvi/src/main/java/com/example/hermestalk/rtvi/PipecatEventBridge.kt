package com.example.hermestalk.rtvi

import ai.pipecat.client.PipecatEventCallbacks
import ai.pipecat.client.transport.MsgServerToClient
import ai.pipecat.client.types.BotReadyData
import ai.pipecat.client.types.Transcript
import ai.pipecat.client.types.TransportState
import ai.pipecat.client.types.Value

/**
 * Translates Pipecat transport callbacks into [ConnectionEvent]s and forwards
 * non-state callbacks (transcripts, audio levels, speech statistics) to
 * [callbacks].
 *
 * Every forwarded event is also written to [CallTrace] so an instrumentation
 * test can assert on the app's own event stream without reading logcat.
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
        CallTrace.record("bot.transcript", mapOf("text" to text.take(MAX_TRACE_TEXT)))
        callbacks.onBotTranscript(text)
    }

    override fun onUserTranscript(transcript: Transcript) {
        val text = transcript.text.take(MAX_TRACE_TEXT)
        CallTrace.record(
            "user.transcript",
            mapOf("text" to text, "final" to transcript.final.toString()),
        )
        callbacks.onUserTranscript(text, transcript.final)
    }

    override fun onBotLLMText(data: MsgServerToClient.Data.BotLLMTextData) {
        val text = data.text.take(MAX_TRACE_TEXT)
        CallTrace.record("bot.llm.text", mapOf("text" to text))
        callbacks.onBotLlmText(text)
    }

    override fun onBotTTSStarted() {
        CallTrace.record("bot.tts.started")
        callbacks.onBotTtsStarted()
    }

    override fun onBotTTSStopped() {
        CallTrace.record("bot.tts.stopped")
        callbacks.onBotTtsStopped()
    }

    override fun onServerMessage(data: Value) {
        val payload = (data as? Value.Object)?.value ?: return
        // Pipecat serialises custom messages with compact keys ("t"/"d") in
        // some versions and with "type"/"data" in others; accept both.
        val type = (payload["type"] as? Value.Str)?.value
            ?: (payload["t"] as? Value.Str)?.value
            ?: return
        CallTrace.record("server.message", mapOf("type" to type))
        if (type != AUDIO_STATS_TYPE) {
            return
        }
        val body = (payload["data"] as? Value.Object)?.value
            ?: (payload["d"] as? Value.Object)?.value
            ?: return
        val audioBytes = (body["audio_bytes"] as? Value.Number)?.value?.toLong() ?: return
        val audioMs = (body["audio_ms"] as? Value.Number)?.value ?: 0.0
        CallTrace.record(
            "bot.audio.stats",
            mapOf("audio_bytes" to audioBytes.toString(), "audio_ms" to audioMs.toString()),
        )
        callbacks.onBotAudioStats(audioBytes, audioMs)
    }

    override fun onUserAudioLevel(level: Float) {
        callbacks.onUserAudioLevel(level)
    }

    private companion object {
        const val MAX_DETAIL = 200
        const val MAX_TRACE_TEXT = 200

        /** Custom server message the relay uses to report pushed speech audio. */
        const val AUDIO_STATS_TYPE = "bot-audio-stats"
    }
}
