package com.example.hermestalk.attachments

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class RetryPolicyTest {

    @Test
    fun backoffScheduleIsBounded() {
        assertEquals(listOf(1L, 2L, 5L, 10L, 20L, 30L, 60L), RetryPolicy.delaysSeconds)
        assertEquals(8, RetryPolicy.MAX_ATTEMPTS)
        assertEquals(1L, RetryPolicy.delayFor(1))
        assertEquals(2L, RetryPolicy.delayFor(2))
        assertEquals(5L, RetryPolicy.delayFor(3))
        assertEquals(10L, RetryPolicy.delayFor(4))
        assertEquals(20L, RetryPolicy.delayFor(5))
        assertEquals(30L, RetryPolicy.delayFor(6))
        assertEquals(60L, RetryPolicy.delayFor(7))
        assertEquals(60L, RetryPolicy.delayFor(20))
    }

    @Test
    fun retryAfterIsAFloor() {
        assertEquals(15L, RetryPolicy.delayFor(1, 15))
        assertEquals(60L, RetryPolicy.delayFor(7, 5))
        assertEquals(90L, RetryPolicy.delayFor(7, 90))
    }

    @Test
    fun transportAndServerFailuresAreRetried() {
        val transport = UploadFailure("transport", "unreachable", retryable = true)
        val busy = UploadFailure("upload_busy", "busy", retryable = true, httpStatus = 503)
        val server = UploadFailure("relay_error", "error", retryable = true, httpStatus = 500)
        assertTrue(RetryPolicy.decide(transport, 1) is RetryDecision.Retry)
        assertTrue(RetryPolicy.decide(busy, 1) is RetryDecision.Retry)
        assertTrue(RetryPolicy.decide(server, 1) is RetryDecision.Retry)
    }

    @Test
    fun terminalRejectionsAreNotRetried() {
        for (status in listOf(400, 401, 403, 409, 413, 415, 422)) {
            val failure = UploadFailure("rejected", "rejected", retryable = true, httpStatus = status)
            assertEquals(RetryDecision.GiveUp, RetryPolicy.decide(failure, 1))
        }
    }

    @Test
    fun stopsAtMaxAttempts() {
        val failure = UploadFailure("transport", "unreachable", retryable = true)
        assertEquals(RetryDecision.GiveUp, RetryPolicy.decide(failure, RetryPolicy.MAX_ATTEMPTS))
        assertTrue(RetryPolicy.decide(failure, RetryPolicy.MAX_ATTEMPTS - 1) is RetryDecision.Retry)
    }
}
