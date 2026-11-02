package com.example.hermestalk.rtvi

/**
 * UI side-effects emitted by [RtviSession]. Every method is a no-op by default
 * so callers only need to override what they use.
 */
interface SessionCallbacks {
    /** Called whenever the connection state changes. */
    fun onStateChanged(state: ConnectionState) {}

    /** Called with a bot transcript fragment. */
    fun onBotTranscript(text: String) {}

    /** Called with a local microphone level in the range 0..1. */
    fun onUserAudioLevel(level: Float) {}

    /** Called with a non-sensitive diagnostic message. */
    fun onLog(message: String) {}

    /**
     * Called when the relay certificate is rejected. [expectedPin] is the pin
     * the app required and [observedPin] is the fingerprint actually presented;
     * either may be null when the failure was an untrusted chain rather than a
     * pin mismatch.
     */
    fun onTlsFailure(expectedPin: String?, observedPin: String?) {}

    /** No-op callbacks. */
    object None : SessionCallbacks
}
