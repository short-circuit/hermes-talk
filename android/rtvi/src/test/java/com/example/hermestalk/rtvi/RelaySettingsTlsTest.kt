package com.example.hermestalk.rtvi

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class RelaySettingsTlsTest {

    private val token = "device-token-abc123"
    private val validPin = TlsTestSupport.pinFor("server-a.crt.pem")

    private fun errors(settings: RelaySettings): List<FieldError> =
        RelaySettingsValidator.validate(settings)

    @Test
    fun httpsWithoutPinIsRejected() {
        val result = errors(RelaySettings("https://relay.example.com", token))
        assertEquals(1, result.size)
        assertEquals(Field.TlsPin, result.first().field)
        assertTrue(result.first().message.contains("Pin the relay certificate"))
    }

    @Test
    fun httpsWithMalformedPinIsRejected() {
        val result = errors(
            RelaySettings(
                "https://relay.example.com",
                token,
                tlsPin = "sha256/not-base64!",
            ),
        )
        assertEquals(1, result.size)
        assertEquals(Field.TlsPin, result.first().field)
        assertTrue(result.first().message.contains("SHA-256"))
    }

    @Test
    fun platformCaOptInNeedsNoPin() {
        val result = errors(
            RelaySettings(
                "https://relay.example.com",
                token,
                trustPlatformCa = true,
            ),
        )
        assertTrue(result.isEmpty())
    }

    @Test
    fun validPinIsAcceptedAndPolicyIsPinned() {
        val settings = RelaySettings("https://relay.example.com", token, validPin)
        assertTrue(errors(settings).isEmpty())
        val policy = settings.tlsPolicy()
        assertTrue(policy is TlsPolicy.Pinned)
        assertEquals(validPin, (policy as TlsPolicy.Pinned).pin)
    }

    @Test
    fun bareBase64PinIsNormalizedInThePolicy() {
        val settings = RelaySettings(
            "https://relay.example.com",
            token,
            validPin.removePrefix("sha256/"),
        )
        assertTrue(errors(settings).isEmpty())
        val policy = settings.tlsPolicy()
        assertTrue(policy is TlsPolicy.Pinned)
        assertEquals(validPin, (policy as TlsPolicy.Pinned).pin)
    }

    @Test
    fun loopbackHttpIsAcceptedAsCleartext() {
        val settings = RelaySettings("http://127.0.0.1:8790", token)
        assertTrue(errors(settings).isEmpty())
        assertEquals(TlsPolicy.CleartextLoopback, settings.tlsPolicy())
    }

    @Test
    fun nonLoopbackHttpIsRejected() {
        val result = errors(RelaySettings("http://relay.example.com:8790", token))
        assertEquals(1, result.size)
        assertEquals(Field.RelayUrl, result.first().field)
        assertTrue(result.first().message.contains("loopback"))
    }

    @Test
    fun pinOnLoopbackHttpIsAllowedButIgnored() {
        val settings = RelaySettings("http://127.0.0.1:8790", token, tlsPin = "not-a-valid-pin")
        assertTrue(errors(settings).isEmpty())
        assertEquals(TlsPolicy.CleartextLoopback, settings.tlsPolicy())
    }
}
