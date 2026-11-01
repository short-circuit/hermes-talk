package com.example.hermestalk.rtvi

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ProbeMappingTest {

    @Test
    fun status200IsOk() {
        assertEquals(ProbeResult.Ok, ProbeMapping.fromStatus(200, "{\"status\":\"ok\"}"))
    }

    @Test
    fun status401IsUnauthorized() {
        val result = ProbeMapping.fromStatus(401, "unauthorized")
        assertEquals(FailureReason.Unauthorized, (result as ProbeResult.Failure).reason)
    }

    @Test
    fun status403IsUnauthorized() {
        val result = ProbeMapping.fromStatus(403, "forbidden")
        assertEquals(FailureReason.Unauthorized, (result as ProbeResult.Failure).reason)
    }

    @Test
    fun status503IsNotReady() {
        val result = ProbeMapping.fromStatus(503, "starting")
        assertEquals(FailureReason.NotReady, (result as ProbeResult.Failure).reason)
    }

    @Test
    fun status500IsUnexpected() {
        val result = ProbeMapping.fromStatus(500, "boom")
        assertEquals(FailureReason.Unexpected, (result as ProbeResult.Failure).reason)
        assertTrue(result.detail.contains("500"))
    }

    @Test
    fun status0IsUnreachable() {
        val result = ProbeMapping.fromStatus(0, "Connection refused")
        assertEquals(FailureReason.Unreachable, (result as ProbeResult.Failure).reason)
        assertEquals("Connection refused", result.detail)
    }

    @Test
    fun ioFailureWithNoSnippetIsUnreachable() {
        val result = ProbeMapping.fromStatus(0, null)
        assertEquals(FailureReason.Unreachable, (result as ProbeResult.Failure).reason)
        assertEquals("Relay unreachable", result.detail)
    }

    @Test
    fun otherSuccessStatusIsOk() {
        assertEquals(ProbeResult.Ok, ProbeMapping.fromStatus(204, null))
    }

    @Test
    fun bodySnippetIsCapped() {
        val longBody = "x".repeat(500)
        val result = ProbeMapping.fromStatus(500, longBody)
        assertTrue((result as ProbeResult.Failure).detail.length <= 200)
    }
}
