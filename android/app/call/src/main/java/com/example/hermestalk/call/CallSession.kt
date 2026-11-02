package com.example.hermestalk.call

import android.content.Context
import com.example.hermestalk.rtvi.ConnectionState
import com.example.hermestalk.rtvi.RelaySettings
import com.example.hermestalk.rtvi.RtviSession
import com.example.hermestalk.rtvi.SessionCallbacks

/** Callback surface used by [CallSession] implementations. */
interface CallSessionCallbacks {
    /** Called whenever the RTVI connection state changes. */
    fun onConnectionState(state: ConnectionState)

    /** Called with a local microphone level in the range 0..1. */
    fun onUserAudioLevel(level: Float)

    /** Called with a bot transcript fragment. */
    fun onBotTranscript(text: String)

    /** Called with a non-sensitive diagnostic message. */
    fun onLog(message: String)
}

/** A connectable, releasable voice session. */
interface CallSession {
    /** Connect to the relay and wait for the session to settle. */
    suspend fun connect(settings: RelaySettings)

    /** Enable or disable the local microphone. */
    fun setMicEnabled(enabled: Boolean)

    /** Disconnect and release the session. The microphone must be released. */
    fun stop()

    /** True when the session is ready for audio. */
    val connected: Boolean
}

/**
 * [RtviSession]-backed call session. Construction must happen on a thread with
 * a Looper, which is why [CallController] creates it from its main dispatcher.
 *
 * The stub relay accepts an empty bearer token, so a blank token is normalized
 * to a local placeholder before [RtviSession] validates it. A real relay that
 * requires a token still rejects the placeholder with 401, which surfaces as
 * [CallFailureReason.RelayRejected].
 */
class RtviCallSession(
    context: Context,
    private val callbacks: CallSessionCallbacks,
) : CallSession {

    private val session = RtviSession(
        context,
        callbacks = object : SessionCallbacks {
            override fun onStateChanged(state: ConnectionState) {
                callbacks.onConnectionState(state)
            }

            override fun onBotTranscript(text: String) {
                callbacks.onBotTranscript(text)
            }

            override fun onUserAudioLevel(level: Float) {
                callbacks.onUserAudioLevel(level)
            }

            override fun onLog(message: String) {
                callbacks.onLog(message)
            }
        },
    )

    @Volatile
    private var stopped = false

    override suspend fun connect(settings: RelaySettings) {
        val normalized = if (settings.deviceToken.isBlank()) {
            settings.copy(deviceToken = LOCAL_PLACEHOLDER_TOKEN)
        } else {
            settings
        }
        session.connect(normalized)
    }

    override fun setMicEnabled(enabled: Boolean) {
        session.updateMic(enabled)
    }

    override fun stop() {
        if (stopped) {
            return
        }
        stopped = true
        try {
            session.disconnect()
        } catch (error: Exception) {
            callbacks.onLog("disconnect failed: ${error.message}")
        }
        try {
            session.release()
        } catch (error: Exception) {
            callbacks.onLog("release failed: ${error.message}")
        }
    }

    override val connected: Boolean
        get() = session.state.value is ConnectionState.Ready

    private companion object {
        const val LOCAL_PLACEHOLDER_TOKEN = "local"
    }
}
