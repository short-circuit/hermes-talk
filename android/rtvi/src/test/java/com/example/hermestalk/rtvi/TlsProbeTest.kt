package com.example.hermestalk.rtvi

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class TlsProbeTest {

    private val pinA = TlsTestSupport.pinFor("server-a.crt.pem")
    private val pinB = TlsTestSupport.pinFor("server-b.crt.pem")
    private val token = "device-token-abc123"

    @Test
    fun pinnedProbeAcceptsTheMatchingLeaf() {
        val server = TlsTestSupport.startTlsServer(
            TlsTestSupport.serverContext("server-a.crt.pem", "server-a.key.b64"),
            respondToHttp = true,
        )
        try {
            val endpoint = endpoint("https://127.0.0.1:${server.port}")
            val result = runBlocking {
                HttpHealthProbe().probe(endpoint, token, TlsPolicy.Pinned(pinA))
            }
            assertEquals(ProbeResult.Ok, result)
        } finally {
            server.close()
        }
    }

    @Test
    fun pinnedProbeRejectsAMismatchedLeaf() {
        val server = TlsTestSupport.startTlsServer(
            TlsTestSupport.serverContext("server-a.crt.pem", "server-a.key.b64"),
            respondToHttp = true,
        )
        try {
            val endpoint = endpoint("https://127.0.0.1:${server.port}")
            val result = runBlocking {
                HttpHealthProbe().probe(endpoint, token, TlsPolicy.Pinned(pinB))
            }
            val failure = result as ProbeResult.Failure
            assertEquals(FailureReason.TlsRejected, failure.reason)
            assertEquals(pinB, failure.expectedPin)
            assertEquals(pinA, failure.observedPin)
            assertTrue(failure.detail.contains(pinB))
            assertTrue(failure.detail.contains(pinA))
        } finally {
            server.close()
        }
    }

    @Test
    fun platformTrustAgainstTheInternalCaIsRejected() {
        val server = TlsTestSupport.startTlsServer(
            TlsTestSupport.serverContext("server-a.crt.pem", "server-a.key.b64"),
            respondToHttp = true,
        )
        try {
            val endpoint = endpoint("https://127.0.0.1:${server.port}")
            val result = runBlocking {
                HttpHealthProbe().probe(endpoint, token, TlsPolicy.PlatformCa)
            }
            val failure = result as ProbeResult.Failure
            assertEquals(FailureReason.TlsRejected, failure.reason)
            assertTrue(
                failure.detail.startsWith("The relay certificate was not trusted:"),
            )
        } finally {
            server.close()
        }
    }

    @Test
    fun transportLevelTlsFailureIsUnreachableNotTrustRejection() {
        val server = TlsTestSupport.startClosingServer()
        try {
            val endpoint = endpoint("https://127.0.0.1:${server.port}")
            val result = runBlocking {
                HttpHealthProbe().probe(endpoint, token, TlsPolicy.Pinned(pinA))
            }
            val failure = result as ProbeResult.Failure
            assertEquals(FailureReason.Unreachable, failure.reason)
            assertNull(failure.expectedPin)
            assertNull(failure.observedPin)
        } finally {
            server.close()
        }
    }

    @Test
    fun cleartextLoopbackProbeSucceeds() {
        val server = TlsTestSupport.startPlainServer(respondToHttp = true)
        try {
            val endpoint = endpoint("http://127.0.0.1:${server.port}")
            val result = runBlocking {
                HttpHealthProbe().probe(endpoint, token, TlsPolicy.CleartextLoopback)
            }
            assertEquals(ProbeResult.Ok, result)
        } finally {
            server.close()
        }
    }

    private fun endpoint(raw: String): RelayEndpoint {
        val parsed = RelayEndpoint.parse(raw)
        assertTrue("expected valid endpoint: $raw", parsed is RelayEndpoint.ParseResult.Valid)
        return (parsed as RelayEndpoint.ParseResult.Valid).endpoint
    }
}
