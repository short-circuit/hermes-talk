package com.example.hermestalk.call

import android.content.Context

/** Restores the audio path after a wake-word detection. */
interface AssistantAudioRestore {
    fun restoreAfterDetection(): Boolean
}

/**
 * Abandons audio focus, clears the communication device and leaves the
 * communication audio mode so a call can take focus and route cleanly.
 *
 * Each platform step is isolated: a failure in one step does not skip the
 * others, and the return value reports whether all three completed.
 */
class AndroidAssistantAudioRestore(context: Context) : AssistantAudioRestore {

    private val appContext = context.applicationContext

    override fun restoreAfterDetection(): Boolean {
        var completed = true
        try {
            AndroidAudioFocusController(appContext).abandon()
        } catch (_: Exception) {
            completed = false
        }

        val routes = AndroidAudioRouteController(appContext)
        try {
            routes.clearDevice()
        } catch (_: Exception) {
            completed = false
        }
        try {
            routes.exitCommunicationMode()
        } catch (_: Exception) {
            completed = false
        }
        return completed
    }
}
