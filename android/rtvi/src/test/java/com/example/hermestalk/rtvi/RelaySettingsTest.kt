package com.example.hermestalk.rtvi

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class RelaySettingsTest {

    private fun errors(settings: RelaySettings): List<FieldError> =
        RelaySettingsValidator.validate(settings)

    @Test
    fun validSettingsHaveNoErrors() {
        val result = errors(
            RelaySettings("http://127.0.0.1:8790", "device-token-abc123"),
        )
        assertTrue(result.isEmpty())
    }

    @Test
    fun shortTokenIsAccepted() {
        val result = errors(RelaySettings("https://relay.example.com", "abc"))
        assertTrue(result.isEmpty())
    }

    @Test
    fun blankTokenIsRejected() {
        val result = errors(RelaySettings("http://127.0.0.1:8790", "   "))
        assertEquals(1, result.size)
        assertEquals(Field.DeviceToken, result.first().field)
        assertTrue(result.first().message.contains("required"))
    }

    @Test
    fun emptyTokenIsRejected() {
        val result = errors(RelaySettings("http://127.0.0.1:8790", ""))
        assertEquals(1, result.size)
        assertEquals(Field.DeviceToken, result.first().field)
    }

    @Test
    fun tokenWithWhitespaceIsRejected() {
        val result = errors(RelaySettings("http://127.0.0.1:8790", "abc def"))
        assertEquals(1, result.size)
        assertEquals(Field.DeviceToken, result.first().field)
        assertTrue(result.first().message.contains("spaces"))
    }

    @Test
    fun invalidRelayUrlIsRejected() {
        val result = errors(RelaySettings("not-a-url", "device-token"))
        assertEquals(1, result.size)
        assertEquals(Field.RelayUrl, result.first().field)
    }

    @Test
    fun placeholderTokenIsRejected() {
        val placeholders = listOf(
            "changeme",
            "your-token",
            "paste-token-here",
            "replace-me",
            "example-token",
        )
        for (token in placeholders) {
            val result = errors(RelaySettings("http://127.0.0.1:8790", token))
            assertEquals("placeholder should fail: $token", 1, result.size)
            assertEquals(Field.DeviceToken, result.first().field)
            assertTrue(result.first().message.contains("example"))
        }
    }

    @Test
    fun reportsBothFieldsWhenBothAreInvalid() {
        val result = errors(RelaySettings("", ""))
        assertEquals(2, result.size)
        assertTrue(result.any { it.field == Field.RelayUrl })
        assertTrue(result.any { it.field == Field.DeviceToken })
    }
}
