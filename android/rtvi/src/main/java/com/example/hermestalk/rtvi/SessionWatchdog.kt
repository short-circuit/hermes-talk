package com.example.hermestalk.rtvi

/**
 * Consecutive-failure watchdog for an established RTVI session.
 *
 * The Pipecat Android SDK only logs ICE connection-state changes and never
 * surfaces a remote hang-up, so a relay that dies without closing the data
 * channel leaves the app in [ConnectionState.Connected] or
 * [ConnectionState.Ready] indefinitely. The session feeds periodic `/health`
 * probe results into this watchdog and treats [Action.DeclareLost] as a closed
 * transport.
 *
 * This class is pure Kotlin with no I/O and no Android dependencies; callers
 * own the scheduling and the actual probing.
 */
class SessionWatchdog(
    private val intervalMs: Long = DEFAULT_INTERVAL_MS,
    private val failureThreshold: Int = DEFAULT_FAILURE_THRESHOLD,
) {

    /** Outcome of recording a probe result. */
    enum class Action {
        /** The session is still considered alive. */
        Continue,

        /** The transport should be treated as closed. */
        DeclareLost,
    }

    /** Delay between liveness probes, in milliseconds. */
    val interval: Long = intervalMs

    private val threshold = failureThreshold.coerceAtLeast(1)
    private var consecutiveFailures = 0

    /**
     * Record a probe outcome. A success resets the failure counter and returns
     * [Action.Continue]; a failure increments it. Reaching the configured
     * threshold returns [Action.DeclareLost] and resets the counter so the next
     * connection starts from a clean slate. Never throws.
     */
    fun onProbeResult(ok: Boolean): Action {
        if (ok) {
            consecutiveFailures = 0
            return Action.Continue
        }

        consecutiveFailures += 1
        if (consecutiveFailures >= threshold) {
            consecutiveFailures = 0
            return Action.DeclareLost
        }
        return Action.Continue
    }

    /** Reset the consecutive failure counter, for example when a new session starts. */
    fun reset() {
        consecutiveFailures = 0
    }

    private companion object {
        const val DEFAULT_INTERVAL_MS = 15_000L
        const val DEFAULT_FAILURE_THRESHOLD = 2
    }
}
