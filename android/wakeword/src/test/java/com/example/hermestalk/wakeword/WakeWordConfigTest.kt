package com.example.hermestalk.wakeword

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class WakeWordConfigTest {

    @Test
    fun defaultsAreValidAndUseMeasuredTuning() {
        val config = WakeWordConfig()
        assertTrue(config.isValid())
        assertEquals("HEY HERMES", config.phrase)
        assertEquals("wakeword", config.assetDirectory)
        assertEquals(WakeWordSensitivity.Balanced, config.sensitivity)
        assertEquals(0.15f, config.threshold, 0.0001f)
        assertEquals(1.0f, config.score, 0.0001f)
        assertEquals(1, config.trailingBlanks)
        assertEquals(4, config.maxActivePaths)
        assertEquals(1, config.numThreads)
        assertEquals(2500L, config.cooldownMillis)
        assertEquals(16000, config.captureSampleRate)
        assertEquals(1600, config.chunkSamples)
        assertEquals(65536L, config.falseTriggerLogCapBytes)
    }

    @Test
    fun presetsMapToThresholds() {
        assertEquals(0.10f, WakeWordSensitivity.Sensitive.threshold, 0.0001f)
        assertEquals(0.15f, WakeWordSensitivity.Balanced.threshold, 0.0001f)
        assertEquals(0.25f, WakeWordSensitivity.Strict.threshold, 0.0001f)
        assertEquals(WakeWordSensitivity.Strict, WakeWordSensitivity.fromStorage("strict"))
        assertEquals(WakeWordSensitivity.Sensitive, WakeWordSensitivity.fromStorage("SENSITIVE"))
        assertEquals(WakeWordSensitivity.Balanced, WakeWordSensitivity.fromStorage("unknown"))
        assertEquals(WakeWordSensitivity.Balanced, WakeWordSensitivity.fromStorage(null))
    }

    @Test
    fun invalidValuesReturnFieldErrorsWithoutThrowing() {
        val config = WakeWordConfig(
            phrase = "",
            assetDirectory = "/absolute/",
            score = -1f,
            trailingBlanks = -1,
            maxActivePaths = 0,
            numThreads = 0,
            cooldownMillis = -1L,
            captureSampleRate = 1,
            chunkSamples = 0,
            falseTriggerLogCapBytes = 1L,
        )
        val errors = config.validate()
        val fields = errors.map { it.field }.toSet()
        assertTrue(WakeWordField.Phrase in fields)
        assertTrue(WakeWordField.AssetDirectory in fields)
        assertTrue(WakeWordField.Score in fields)
        assertTrue(WakeWordField.TrailingBlanks in fields)
        assertTrue(WakeWordField.MaxActivePaths in fields)
        assertTrue(WakeWordField.NumThreads in fields)
        assertTrue(WakeWordField.CooldownMillis in fields)
        assertTrue(WakeWordField.SampleRate in fields)
        assertTrue(WakeWordField.ChunkSamples in fields)
        assertTrue(WakeWordField.LogCapBytes in fields)
        assertFalse(config.isValid())
    }

    @Test
    fun multilinePhraseAndParentTraversalAreRejected() {
        val config = WakeWordConfig(
            phrase = "HEY\nHERMES",
            assetDirectory = "../wakeword",
        )
        val fields = config.validate().map { it.field }.toSet()
        assertTrue(WakeWordField.Phrase in fields)
        assertTrue(WakeWordField.AssetDirectory in fields)
    }

    @Test
    fun chunkLargerThanSampleRateIsRejected() {
        val config = WakeWordConfig(captureSampleRate = 16000, chunkSamples = 16001)
        assertTrue(WakeWordField.ChunkSamples in config.validate().map { it.field })
    }
}
