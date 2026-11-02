package com.example.hermestalk

import com.example.hermestalk.rtvi.Field
import com.example.hermestalk.rtvi.InMemoryTokenStore
import com.example.hermestalk.rtvi.RelaySettings
import java.io.File
import java.util.Base64
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class TlsSettingsTest {

    private val token = "device-token-abc123"
    private val validPin = "sha256/" +
        Base64.getEncoder().encodeToString(ByteArray(32) { (it + 1).toByte() })

    @Test
    fun repositoryRoundTripsTlsPin() {
        val store = InMemoryTokenStore()
        val repository = SettingsRepository(store)
        val settings = RelaySettings("https://relay.example.com", token, validPin)

        val errors = repository.save(settings)

        assertTrue(errors.isEmpty())
        assertEquals(settings, repository.load())
        assertEquals(settings, store.read())
    }

    @Test
    fun repositoryRoundTripsPlatformCaFlag() {
        val store = InMemoryTokenStore()
        val repository = SettingsRepository(store)
        val settings = RelaySettings(
            "https://relay.example.com",
            token,
            trustPlatformCa = true,
        )

        val errors = repository.save(settings)

        assertTrue(errors.isEmpty())
        assertEquals(settings, repository.load())
        assertEquals(settings, store.read())
    }

    @Test
    fun malformedPinIsNotWritten() {
        val store = InMemoryTokenStore()
        val repository = SettingsRepository(store)

        val errors = repository.save(
            RelaySettings("https://relay.example.com", token, "sha256/not-base64!"),
        )

        assertEquals(1, errors.size)
        assertEquals(Field.TlsPin, errors.first().field)
        assertNull(store.read())
        assertNull(repository.load())
    }

    @Test
    fun sourceTreeHasNoTrustAllManager() {
        val roots = listOf(
            sourceRoot("../rtvi/src/main/java", "rtvi/src/main/java"),
            sourceRoot("src/main/java", "app/src/main/java"),
        )
        val allowed = setOf("PinnedTrustManager", "CapturingTrustManager")
        val implementation = Regex(
            """(?:class|object)\s+([A-Za-z_][A-Za-z0-9_]*)[^\n]*X509ExtendedTrustManager""",
        )
        var implementations = 0

        for (root in roots) {
            root.walkTopDown()
                .filter { it.isFile && it.extension == "kt" }
                .forEach { file ->
                    val text = file.readText()
                    implementation.findAll(text).forEach { match ->
                        implementations++
                        val name = match.groupValues[1]
                        assertTrue(
                            "Unexpected X509ExtendedTrustManager implementation $name in ${file.path}",
                            name in allowed,
                        )
                    }
                    assertFalse("trustAll identifier in ${file.path}", text.contains("trustAll"))
                    assertFalse("TrustAll identifier in ${file.path}", text.contains("TrustAll"))
                }
        }

        assertTrue(
            "Expected both PinnedTrustManager and CapturingTrustManager in the source tree",
            implementations >= 2,
        )
    }

    private fun sourceRoot(vararg candidates: String): File {
        for (candidate in candidates) {
            val file = File(candidate)
            if (file.isDirectory) {
                return file
            }
        }
        error("None of these source roots exist: ${candidates.joinToString()}")
    }
}
