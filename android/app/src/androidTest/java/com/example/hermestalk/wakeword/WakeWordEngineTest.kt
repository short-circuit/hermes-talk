package com.example.hermestalk.wakeword

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class WakeWordEngineTest {

    private val positives = listOf(
        "hey_hermes_af_heart.wav",
        "hey_hermes_af_bella.wav",
        "hey_hermes_am_michael.wav",
        "hey_hermes_am_adam.wav",
        "hey_hermes_bf_emma.wav",
        "hey_hermes_bm_george.wav",
        "hey_hermes_af_jessica.wav",
        "hey_hermes_bf_alice.wav",
        "hey_hermes_af_nicole.wav",
        "hey_hermes_bm_lewis.wav",
    )

    private val negatives = listOf(
        "negative_af_heart.wav",
        "negative_am_michael.wav",
        "negative_bf_emma.wav",
    )

    @Test
    fun fixturesDetectAtLeastNinetyPercentAndRejectNegatives() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val targetContext = instrumentation.targetContext
        val testAssets = instrumentation.context.assets
        val config = WakeWordConfig()
        val engine = WakeWordEngine.create(targetContext, config)

        try {
            var detected = 0
            for (file in positives) {
                val fixture = readWavFixture(testAssets, "wakeword/$file")
                engine.newStream()
                var offset = 0
                var hit = false
                while (offset < fixture.samples.size) {
                    val end = minOf(offset + config.chunkSamples, fixture.samples.size)
                    engine.accept(fixture.samples.copyOfRange(offset, end), fixture.sampleRate)
                    val keyword = engine.poll()
                    if (keyword != null && normalizeKeyword(keyword) == "HEYHERMES") {
                        hit = true
                        break
                    }
                    offset = end
                }
                if (hit) {
                    detected += 1
                }
            }
            assertTrue(
                "Detected $detected of ${positives.size} positive fixtures",
                detected >= 9,
            )

            for (file in negatives) {
                val fixture = readWavFixture(testAssets, "wakeword/$file")
                engine.newStream()
                var offset = 0
                var keyword: String? = null
                while (offset < fixture.samples.size && keyword == null) {
                    val end = minOf(offset + config.chunkSamples, fixture.samples.size)
                    engine.accept(fixture.samples.copyOfRange(offset, end), fixture.sampleRate)
                    keyword = engine.poll()
                    offset = end
                }
                assertFalse(
                    "$file unexpectedly detected ${keyword ?: "nothing"}",
                    keyword != null && normalizeKeyword(keyword) == "HEYHERMES",
                )
            }
        } finally {
            engine.release()
            engine.release()
        }
    }
}
