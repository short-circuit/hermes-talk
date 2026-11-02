package com.example.hermestalk.call

import android.content.Context
import android.media.AudioManager
import android.media.AudioRecordingConfiguration
import android.media.MediaRecorder
import android.os.Handler
import android.os.Looper

/** Reports whether this app currently has an active recording. */
interface MicActivityMonitor {
    /** Start observing recording activity. */
    fun start(onChanged: (Boolean) -> Unit)

    /** Stop observing recording activity. */
    fun stop()

    /** True when one of this app's recording configurations is active. */
    fun isRecording(): Boolean
}

/**
 * [AudioManager] recording-callback monitor. The platform's
 * [AudioRecordingConfiguration] does not expose a client package name, so
 * configurations using the voice-communication or microphone source count as
 * our microphone being live.
 */
class AndroidMicActivityMonitor(context: Context) : MicActivityMonitor {

    private val appContext = context.applicationContext
    private val audioManager =
        appContext.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private var callback: ((Boolean) -> Unit)? = null
    private var registered = false

    private val recordingCallback = object : AudioManager.AudioRecordingCallback() {
        override fun onRecordingConfigChanged(configs: List<AudioRecordingConfiguration>?) {
            callback?.invoke(isRecording())
        }
    }

    override fun start(onChanged: (Boolean) -> Unit) {
        callback = onChanged
        if (!registered) {
            try {
                audioManager.registerAudioRecordingCallback(
                    recordingCallback,
                    Handler(Looper.getMainLooper()),
                )
                registered = true
            } catch (_: Exception) {
                registered = false
            }
        }
        onChanged(isRecording())
    }

    override fun stop() {
        if (registered) {
            try {
                audioManager.unregisterAudioRecordingCallback(recordingCallback)
            } catch (_: Exception) {
                // Ignore an already-unregistered callback.
            }
        }
        registered = false
        callback = null
    }

    override fun isRecording(): Boolean {
        return try {
            audioManager.getActiveRecordingConfigurations().any {
                it.clientAudioSource == MediaRecorder.AudioSource.VOICE_COMMUNICATION ||
                    it.clientAudioSource == MediaRecorder.AudioSource.MIC
            }
        } catch (_: Exception) {
            false
        }
    }
}
