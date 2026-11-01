package com.example.hermestalk.rtvi

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RelayEndpointTest {

    private fun valid(raw: String): RelayEndpoint {
        val parsed = RelayEndpoint.parse(raw)
        assertTrue("expected valid: $raw", parsed is RelayEndpoint.ParseResult.Valid)
        return (parsed as RelayEndpoint.ParseResult.Valid).endpoint
    }

    private fun invalid(raw: String): String {
        val parsed = RelayEndpoint.parse(raw)
        assertTrue("expected invalid: $raw", parsed is RelayEndpoint.ParseResult.Invalid)
        return (parsed as RelayEndpoint.ParseResult.Invalid).reason
    }

    @Test
    fun parsesHttpUrlWithPort() {
        val endpoint = valid("http://127.0.0.1:8790")
        assertEquals("http", endpoint.scheme)
        assertEquals("127.0.0.1", endpoint.host)
        assertEquals(8790, endpoint.port)
        assertEquals("", endpoint.path)
        assertEquals("http://127.0.0.1:8790", endpoint.baseUrl())
        assertEquals("http://127.0.0.1:8790/start", endpoint.startUrl())
        assertEquals("http://127.0.0.1:8790/health", endpoint.healthUrl())
    }

    @Test
    fun parsesHttpsUrlWithoutPort() {
        val endpoint = valid("https://relay.example.com")
        assertEquals("https", endpoint.scheme)
        assertEquals("relay.example.com", endpoint.host)
        assertEquals(null, endpoint.port)
        assertEquals("https://relay.example.com", endpoint.baseUrl())
        assertFalse(endpoint.isCleartext)
    }

    @Test
    fun normalizesTrailingSlash() {
        assertEquals("http://relay.example.com:8080", valid("http://relay.example.com:8080/").baseUrl())
        assertEquals("http://relay.example.com:8080/api", valid("http://relay.example.com:8080/api/").baseUrl())
    }

    @Test
    fun keepsPathPrefix() {
        val endpoint = valid("https://relay.example.com/hermes/")
        assertEquals("/hermes", endpoint.path)
        assertEquals("https://relay.example.com/hermes/start", endpoint.startUrl())
    }

    @Test
    fun normalizesUppercaseScheme() {
        val endpoint = valid("HTTP://127.0.0.1:8790")
        assertEquals("http", endpoint.scheme)
        assertEquals("http://127.0.0.1:8790", endpoint.baseUrl())
    }

    @Test
    fun rejectsBlankInput() {
        assertTrue(invalid("").isNotBlank())
        assertTrue(invalid("   ").isNotBlank())
    }

    @Test
    fun rejectsMissingScheme() {
        assertTrue(invalid("127.0.0.1:8790").isNotBlank())
        assertTrue(invalid("relay.example.com").isNotBlank())
    }

    @Test
    fun rejectsUnsupportedScheme() {
        assertTrue(invalid("ftp://relay.example.com").contains("http"))
    }

    @Test
    fun rejectsBadPort() {
        assertTrue(invalid("http://relay.example.com:0").isNotBlank())
        assertTrue(invalid("http://relay.example.com:70000").isNotBlank())
        assertTrue(invalid("http://relay.example.com:-1").isNotBlank())
    }

    @Test
    fun rejectsUserinfo() {
        assertTrue(invalid("http://user:pass@example.com:8790").contains("credentials"))
    }

    @Test
    fun rejectsQueryAndFragment() {
        assertTrue(invalid("http://relay.example.com:8790/?token=abc").contains("query"))
        assertTrue(invalid("http://relay.example.com:8790/#section").contains("fragment"))
    }

    @Test
    fun detectsLoopback() {
        assertTrue(valid("http://127.0.0.1:8790").isLoopback)
        assertTrue(valid("http://localhost:8790").isLoopback)
        assertTrue(valid("http://[::1]:8790").isLoopback)
        assertFalse(valid("http://relay.example.com:8790").isLoopback)
    }

    @Test
    fun detectsCleartext() {
        assertTrue(valid("http://relay.example.com:8790").isCleartext)
        assertFalse(valid("https://relay.example.com:8790").isCleartext)
    }

    @Test
    fun preservesIpv6BracketsInBaseUrl() {
        val endpoint = valid("http://[::1]:8790")
        assertEquals("::1", endpoint.host)
        assertEquals("http://[::1]:8790", endpoint.baseUrl())
    }

    @Test
    fun rejectsMissingHost() {
        assertTrue(invalid("http:///health").isNotBlank())
        assertTrue(invalid("http://:8790").isNotBlank())
    }
}
