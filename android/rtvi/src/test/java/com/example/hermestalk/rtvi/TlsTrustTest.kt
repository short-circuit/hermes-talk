package com.example.hermestalk.rtvi

import java.security.MessageDigest
import java.security.cert.X509Certificate
import java.util.Base64
import javax.net.ssl.SSLException
import javax.net.ssl.SSLSocket
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class TlsTrustTest {

    private val certificateA = TlsTestSupport.certificate("server-a.crt.pem")
    private val pinA = SpkiPin.fromCertificate(certificateA)
    private val pinB = TlsTestSupport.pinFor("server-b.crt.pem")

    @Test
    fun normalizeAcceptsCanonicalPin() {
        assertEquals(pinA, SpkiPin.normalize(pinA))
        assertEquals(pinA, SpkiPin.normalize("  $pinA  "))
    }

    @Test
    fun normalizeAcceptsBareBase64() {
        val bare = pinA.removePrefix("sha256/")
        assertEquals(pinA, SpkiPin.normalize(bare))
    }

    @Test
    fun normalizeAcceptsHexWithSeparators() {
        val digest = MessageDigest.getInstance("SHA-256").digest(certificateA.publicKey.encoded)
        val colonHex = digest.joinToString(":") { "%02x".format(it.toInt() and 0xff) }
        val spaceHex = digest.joinToString(" ") { "%02x".format(it.toInt() and 0xff) }
        val plainHex = digest.joinToString("") { "%02x".format(it.toInt() and 0xff) }
        assertEquals(pinA, SpkiPin.normalize(colonHex))
        assertEquals(pinA, SpkiPin.normalize(spaceHex))
        assertEquals(pinA, SpkiPin.normalize(plainHex))
        assertEquals(pinA, SpkiPin.normalize(plainHex.uppercase()))
    }

    @Test
    fun normalizeRejectsGarbageAndWrongLengthDigests() {
        assertNull(SpkiPin.normalize(""))
        assertNull(SpkiPin.normalize("sha256/"))
        assertNull(SpkiPin.normalize("not-a-pin"))
        assertNull(SpkiPin.normalize("sha256/not-base64!"))
        assertNull(SpkiPin.normalize(Base64.getEncoder().encodeToString(ByteArray(31))))
        assertNull(SpkiPin.normalize(Base64.getEncoder().encodeToString(ByteArray(33))))
        assertNull(SpkiPin.normalize("0".repeat(63)))
        assertNull(SpkiPin.normalize("0".repeat(65)))
    }

    @Test
    fun certificatePinsAreDerivedFromThePublicKey() {
        assertEquals(pinA, SpkiPin.fromCertificate(certificateA))
        assertNotEquals(pinA, SpkiPin.certificateSha256(certificateA))
        assertTrue(SpkiPin.certificateSha256(certificateA).startsWith("sha256/"))
        assertTrue(SpkiPin.matches(pinA, certificateA))
        assertFalse(SpkiPin.matches(pinB, certificateA))
    }

    @Test
    fun pinnedClientCompletesHandshakeWithMatchingLeaf() {
        val server = TlsTestSupport.startTlsServer(
            TlsTestSupport.serverContext("server-a.crt.pem", "server-a.key.b64"),
            respondToHttp = false,
        )
        try {
            val socket = TlsTrust.sslContextForPin(pinA)
                .socketFactory
                .createSocket("127.0.0.1", server.port) as SSLSocket
            socket.use {
                it.soTimeout = 5000
                it.startHandshake()
                val peer = it.session.peerCertificates.first() as X509Certificate
                assertEquals(pinA, SpkiPin.fromCertificate(peer))
            }
        } finally {
            server.close()
        }
    }

    @Test
    fun mismatchedPinFailsHandshakeWithBothFingerprints() {
        val server = TlsTestSupport.startTlsServer(
            TlsTestSupport.serverContext("server-a.crt.pem", "server-a.key.b64"),
            respondToHttp = false,
        )
        try {
            val socket = TlsTrust.sslContextForPin(pinB)
                .socketFactory
                .createSocket("127.0.0.1", server.port) as SSLSocket
            val failure = try {
                socket.use {
                    it.soTimeout = 5000
                    it.startHandshake()
                }
                null
            } catch (error: SSLException) {
                error
            }

            assertNotNull("handshake should fail for a mismatched pin", failure)
            val mismatch = findPinMismatch(failure)
            assertNotNull("failure should carry PinMismatchException", mismatch)
            assertEquals(pinB, mismatch!!.expectedPin)
            assertEquals(pinA, mismatch.observedPin)
            assertTrue(mismatch.message!!.contains(pinB))
            assertTrue(mismatch.message!!.contains(pinA))
        } finally {
            server.close()
        }
    }

    @Test
    fun discoverReadsTheRelayCertificate() {
        val server = TlsTestSupport.startTlsServer(
            TlsTestSupport.serverContext("server-a.crt.pem", "server-a.key.b64"),
            respondToHttp = false,
        )
        try {
            val discovery = TlsTrust.discover("127.0.0.1", server.port, 5000)
            assertEquals(pinA, discovery.spkiPin)
            assertEquals(
                TlsTestSupport.certificateShaFor("server-a.crt.pem"),
                discovery.certificateSha256,
            )
            assertEquals("CN=relay-test-a", discovery.subject)
            assertEquals("CN=Hermes Talk Test CA", discovery.issuer)
            assertFalse(discovery.expired)
            assertTrue(discovery.notAfterEpochMs > System.currentTimeMillis())
        } finally {
            server.close()
        }
    }

    @Test
    fun platformTrustRejectsTheInternalCa() {
        val server = TlsTestSupport.startTlsServer(
            TlsTestSupport.serverContext("server-a.crt.pem", "server-a.key.b64"),
            respondToHttp = false,
        )
        try {
            val socket = TlsTrust.sslContextPlatformTrust()
                .socketFactory
                .createSocket("127.0.0.1", server.port) as SSLSocket
            var failed = false
            try {
                socket.use {
                    it.soTimeout = 5000
                    it.startHandshake()
                }
            } catch (_: SSLException) {
                failed = true
            }
            assertTrue("platform trust must not accept the internal CA", failed)
        } finally {
            server.close()
        }
    }

    private fun findPinMismatch(error: Throwable?): PinMismatchException? {
        var current = error
        val seen = mutableSetOf<Throwable>()
        while (current != null && seen.add(current)) {
            if (current is PinMismatchException) {
                return current
            }
            current = current.cause
        }
        return null
    }
}
