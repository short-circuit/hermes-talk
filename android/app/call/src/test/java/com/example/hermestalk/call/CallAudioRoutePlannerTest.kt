package com.example.hermestalk.call

import org.junit.Assert.assertEquals
import org.junit.Test

class CallAudioRoutePlannerTest {

    @Test
    fun planKeepsPreferredWhenAvailable() {
        assertEquals(
            AudioRoute.Speaker,
            CallAudioRoutePlanner.plan(
                AudioRoute.Speaker,
                listOf(AudioRoute.Earpiece, AudioRoute.Speaker),
            ),
        )
    }

    @Test
    fun planPrefersBluetoothThenWiredThenSpeakerThenEarpiece() {
        assertEquals(
            AudioRoute.Bluetooth,
            CallAudioRoutePlanner.plan(
                AudioRoute.Earpiece,
                listOf(
                    AudioRoute.Earpiece,
                    AudioRoute.Speaker,
                    AudioRoute.WiredHeadset,
                    AudioRoute.Bluetooth,
                ),
            ),
        )
        assertEquals(
            AudioRoute.WiredHeadset,
            CallAudioRoutePlanner.plan(
                AudioRoute.Earpiece,
                listOf(AudioRoute.Earpiece, AudioRoute.Speaker, AudioRoute.WiredHeadset),
            ),
        )
        assertEquals(
            AudioRoute.Speaker,
            CallAudioRoutePlanner.plan(
                AudioRoute.Earpiece,
                listOf(AudioRoute.Earpiece, AudioRoute.Speaker),
            ),
        )
        assertEquals(
            AudioRoute.Earpiece,
            CallAudioRoutePlanner.plan(AudioRoute.Earpiece, listOf(AudioRoute.Earpiece)),
        )
    }

    @Test
    fun planFallsBackToEarpieceWhenNothingIsAvailable() {
        assertEquals(AudioRoute.Earpiece, CallAudioRoutePlanner.plan(AudioRoute.Bluetooth, emptyList()))
    }

    @Test
    fun fallbackFromHeadsetPrefersEarpieceForPrivacy() {
        assertEquals(
            AudioRoute.Earpiece,
            CallAudioRoutePlanner.fallback(
                AudioRoute.Bluetooth,
                listOf(AudioRoute.Earpiece, AudioRoute.Speaker),
            ),
        )
        assertEquals(
            AudioRoute.Earpiece,
            CallAudioRoutePlanner.fallback(
                AudioRoute.WiredHeadset,
                listOf(AudioRoute.Earpiece, AudioRoute.Speaker),
            ),
        )
    }

    @Test
    fun fallbackFromHeadsetUsesSpeakerWhenEarpieceIsMissing() {
        assertEquals(
            AudioRoute.Speaker,
            CallAudioRoutePlanner.fallback(
                AudioRoute.Bluetooth,
                listOf(AudioRoute.Speaker),
            ),
        )
    }

    @Test
    fun fallbackFromSpeakerKeepsSpeakerWhenAvailable() {
        assertEquals(
            AudioRoute.Speaker,
            CallAudioRoutePlanner.fallback(
                AudioRoute.Speaker,
                listOf(AudioRoute.Earpiece, AudioRoute.Speaker),
            ),
        )
    }

    @Test
    fun fallbackDelegatesToPlanWhenPreferredIsGone() {
        assertEquals(
            AudioRoute.WiredHeadset,
            CallAudioRoutePlanner.fallback(
                AudioRoute.Speaker,
                listOf(AudioRoute.Earpiece, AudioRoute.WiredHeadset),
            ),
        )
        assertEquals(
            AudioRoute.Earpiece,
            CallAudioRoutePlanner.fallback(AudioRoute.Earpiece, emptyList()),
        )
    }
}
