package com.example.hermestalk

import com.example.hermestalk.rtvi.Field
import com.example.hermestalk.rtvi.InMemoryTokenStore
import com.example.hermestalk.rtvi.RelaySettings
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SettingsRepositoryTest {

    @Test
    fun saveLoadClearRoundTrip() {
        val store = InMemoryTokenStore()
        val repository = SettingsRepository(store)
        val settings = RelaySettings("http://127.0.0.1:8790", "device-token-abc123")

        val errors = repository.save(settings)

        assertTrue(errors.isEmpty())
        assertEquals(settings, repository.load())
        assertEquals(settings, store.read())

        repository.clear()

        assertNull(repository.load())
        assertNull(store.read())
    }

    @Test
    fun blankTokenIsRejectedBeforeWriting() {
        val store = InMemoryTokenStore()
        val repository = SettingsRepository(store)

        val errors = repository.save(
            RelaySettings("http://127.0.0.1:8790", "   "),
        )

        assertEquals(1, errors.size)
        assertEquals(Field.DeviceToken, errors.first().field)
        assertNull(store.read())
        assertNull(repository.load())
    }

    @Test
    fun whitespaceTokenIsRejectedBeforeWriting() {
        val store = InMemoryTokenStore()
        val repository = SettingsRepository(store)

        val errors = repository.save(
            RelaySettings("http://127.0.0.1:8790", "abc def"),
        )

        assertEquals(1, errors.size)
        assertEquals(Field.DeviceToken, errors.first().field)
        assertNull(store.read())
    }

    @Test
    fun invalidUrlIsRejectedBeforeWriting() {
        val store = InMemoryTokenStore()
        val repository = SettingsRepository(store)

        val errors = repository.save(RelaySettings("not-a-url", "device-token"))

        assertEquals(1, errors.size)
        assertEquals(Field.RelayUrl, errors.first().field)
        assertNull(store.read())
    }

    @Test
    fun failedSaveKeepsPreviousValue() {
        val store = InMemoryTokenStore()
        val repository = SettingsRepository(store)
        val valid = RelaySettings("http://127.0.0.1:8790", "device-token-abc123")

        repository.save(valid)
        repository.save(RelaySettings("http://127.0.0.1:8790", ""))

        assertEquals(valid, store.read())
    }
}
