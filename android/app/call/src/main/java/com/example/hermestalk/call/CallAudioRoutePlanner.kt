package com.example.hermestalk.call

/**
 * Chooses an audio route from the routes the platform currently offers.
 * Bluetooth and wired headsets are preferred over the speaker because they keep
 * the conversation private; the earpiece is the last fallback.
 */
object CallAudioRoutePlanner {

    private val priority = listOf(
        AudioRoute.Bluetooth,
        AudioRoute.WiredHeadset,
        AudioRoute.Speaker,
        AudioRoute.Earpiece,
    )

    /**
     * Return the highest-priority available route. [preferred] is accepted for
     * call-site symmetry but does not override the privacy ordering above.
     */
    fun plan(preferred: AudioRoute, available: List<AudioRoute>): AudioRoute {
        return priority.firstOrNull { it in available } ?: AudioRoute.Earpiece
    }

    /**
     * Choose a route after [preferred] disappeared. Losing a headset falls back
     * to the earpiece for privacy, while an explicit speaker selection is kept
     * when the speaker is still available.
     */
    fun fallback(preferred: AudioRoute, available: List<AudioRoute>): AudioRoute = when (preferred) {
        AudioRoute.Bluetooth,
        AudioRoute.WiredHeadset,
        -> when {
            AudioRoute.Earpiece in available -> AudioRoute.Earpiece
            AudioRoute.Speaker in available -> AudioRoute.Speaker
            else -> plan(preferred, available)
        }

        AudioRoute.Speaker -> if (AudioRoute.Speaker in available) {
            AudioRoute.Speaker
        } else {
            plan(preferred, available)
        }

        AudioRoute.Earpiece -> plan(preferred, available)
    }
}
