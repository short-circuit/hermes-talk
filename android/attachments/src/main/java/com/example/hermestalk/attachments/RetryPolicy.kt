package com.example.hermestalk.attachments

/** Bounded retry decision for one upload failure. */
sealed interface RetryDecision {
    data class Retry(val delaySeconds: Long) : RetryDecision

    data object GiveUp : RetryDecision
}

/**
 * Pure upload retry policy.
 *
 * Retryable: transport failures, timeouts, HTTP 5xx and 503. Terminal:
 * 400, 401, 403, 409, 413, 415 and 422. The backoff schedule is 1, 2, 5, 10,
 * 20, 30 and 60 seconds, with at most [MAX_ATTEMPTS] total attempts. A relay
 * `Retry-After` value is honoured as a floor, never as a shorter delay.
 *
 * This is intentionally conservative: a terminal relay rejection is not retried
 * automatically, because the bytes will not change. The user can still press
 * retry after fixing the cause.
 */
object RetryPolicy {

    val delaysSeconds: List<Long> = listOf(1L, 2L, 5L, 10L, 20L, 30L, 60L)

    const val MAX_ATTEMPTS = 8

    private val terminalStatuses = setOf(400, 401, 403, 409, 413, 415, 422)

    /**
     * Decide whether another attempt should follow [attempt] (the number of
     * attempts already made, starting at 1).
     */
    fun decide(failure: UploadFailure, attempt: Int): RetryDecision {
        if (attempt >= MAX_ATTEMPTS) {
            return RetryDecision.GiveUp
        }
        if (failure.httpStatus in terminalStatuses) {
            return RetryDecision.GiveUp
        }
        if (!failure.retryable) {
            return RetryDecision.GiveUp
        }
        return RetryDecision.Retry(delayFor(attempt, failure.retryAfterSeconds))
    }

    /** Delay before attempt [attempt] + 1, never shorter than [retryAfterSeconds]. */
    fun delayFor(attempt: Int, retryAfterSeconds: Long? = null): Long {
        val index = (attempt - 1).coerceIn(0, delaysSeconds.lastIndex)
        val scheduled = delaysSeconds[index]
        val floor = retryAfterSeconds?.coerceAtLeast(0) ?: 0
        return maxOf(scheduled, floor)
    }
}
