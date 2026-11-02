package com.example.hermestalk.wakeword

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class WakeWordSettingsStoreTest {

    @Test
    fun inMemoryStoreRoundTripsSettings() {
        val store = InMemoryWakeWordSettingsStore()
        assertEquals(WakeWordSettings(), store.load())

        store.save(WakeWordSettings(enabled = true, sensitivity = WakeWordSensitivity.Strict))
        val loaded = store.load()
        assertTrue(loaded.enabled)
        assertEquals(WakeWordSensitivity.Strict, loaded.sensitivity)

        store.save(WakeWordSettings(enabled = false, sensitivity = WakeWordSensitivity.Sensitive))
        val reset = store.load()
        assertFalse(reset.enabled)
        assertEquals(WakeWordSensitivity.Sensitive, reset.sensitivity)
    }

    @Test
    fun initialSettingsArePreserved() {
        val store = InMemoryWakeWordSettingsStore(
            WakeWordSettings(enabled = true, sensitivity = WakeWordSensitivity.Sensitive),
        )
        val loaded = store.load()
        assertTrue(loaded.enabled)
        assertEquals(WakeWordSensitivity.Sensitive, loaded.sensitivity)
    }
}
