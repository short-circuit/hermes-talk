package com.example.hermestalk.call

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/** Requests and releases audio focus for a voice call. */
interface AudioFocusController {
    /** Request focus and return the outcome. */
    fun request(): AudioFocusOutcome

    /** Abandon focus and release the request. */
    fun abandon()

    /** Latest focus outcome, including asynchronous focus changes. */
    val outcome: StateFlow<AudioFocusOutcome>
}

/**
 * Audio-focus controller backed by [AudioManager]. Focus changes are mapped to
 * [AudioFocusOutcome] values and published through [outcome].
 */
class AndroidAudioFocusController(context: Context) : AudioFocusController {

    private val appContext = context.applicationContext
    private val audioManager =
        appContext.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private val _outcome = MutableStateFlow(AudioFocusOutcome.Denied)

    override val outcome: StateFlow<AudioFocusOutcome> = _outcome.asStateFlow()

    private var focusRequest: AudioFocusRequest? = null
    private var listener: AudioManager.OnAudioFocusChangeListener? = null

    override fun request(): AudioFocusOutcome {
        return try {
            val attributes = AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                .build()
            val focusListener = AudioManager.OnAudioFocusChangeListener { change ->
                _outcome.value = mapFocusChange(change)
            }
            listener = focusListener
            val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
                .setAudioAttributes(attributes)
                .setAcceptsDelayedFocusGain(false)
                .setOnAudioFocusChangeListener(focusListener)
                .build()
            focusRequest = request
            val result = audioManager.requestAudioFocus(request)
            val outcome = if (result == AudioManager.AUDIOFOCUS_REQUEST_GRANTED) {
                AudioFocusOutcome.Gained
            } else {
                AudioFocusOutcome.Denied
            }
            _outcome.value = outcome
            outcome
        } catch (_: Exception) {
            _outcome.value = AudioFocusOutcome.Denied
            AudioFocusOutcome.Denied
        }
    }

    override fun abandon() {
        try {
            focusRequest?.let { audioManager.abandonAudioFocusRequest(it) }
        } catch (_: Exception) {
            // Best effort: losing focus must never crash the call.
        }
        focusRequest = null
        listener = null
        _outcome.value = AudioFocusOutcome.Denied
    }

    private fun mapFocusChange(change: Int): AudioFocusOutcome = when (change) {
        AudioManager.AUDIOFOCUS_LOSS -> AudioFocusOutcome.Lost
        AudioManager.AUDIOFOCUS_LOSS_TRANSIENT -> AudioFocusOutcome.LostTransient
        AudioManager.AUDIOFOCUS_GAIN -> AudioFocusOutcome.Gained
        AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK -> AudioFocusOutcome.Duck
        else -> AudioFocusOutcome.Denied
    }
}
