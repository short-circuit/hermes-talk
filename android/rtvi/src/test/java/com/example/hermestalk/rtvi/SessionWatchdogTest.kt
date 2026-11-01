package com.example.hermestalk.rtvi

import org.junit.Assert.assertEquals
import org.junit.Test

class SessionWatchdogTest {

    @Test
    fun defaultConfigurationUsesFifteenSecondIntervalAndTwoFailures() {
        val watchdog = SessionWatchdog()
        assertEquals(15_000L, watchdog.interval)
        assertEquals(SessionWatchdog.Action.Continue, watchdog.onProbeResult(false))
        assertEquals(SessionWatchdog.Action.DeclareLost, watchdog.onProbeResult(false))
    }

    @Test
    fun singleFailureBelowThresholdContinues() {
        val watchdog = SessionWatchdog(failureThreshold = 2)
        assertEquals(SessionWatchdog.Action.Continue, watchdog.onProbeResult(false))
    }

    @Test
    fun reachingThresholdDeclaresLost() {
        val watchdog = SessionWatchdog(failureThreshold = 3)
        assertEquals(SessionWatchdog.Action.Continue, watchdog.onProbeResult(false))
        assertEquals(SessionWatchdog.Action.Continue, watchdog.onProbeResult(false))
        assertEquals(SessionWatchdog.Action.DeclareLost, watchdog.onProbeResult(false))
    }

    @Test
    fun successResetsConsecutiveFailures() {
        val watchdog = SessionWatchdog(failureThreshold = 2)
        assertEquals(SessionWatchdog.Action.Continue, watchdog.onProbeResult(false))
        assertEquals(SessionWatchdog.Action.Continue, watchdog.onProbeResult(true))
        assertEquals(SessionWatchdog.Action.Continue, watchdog.onProbeResult(false))
        assertEquals(SessionWatchdog.Action.DeclareLost, watchdog.onProbeResult(false))
    }

    @Test
    fun counterResetsAfterDeclaringLost() {
        val watchdog = SessionWatchdog(failureThreshold = 2)
        assertEquals(SessionWatchdog.Action.Continue, watchdog.onProbeResult(false))
        assertEquals(SessionWatchdog.Action.DeclareLost, watchdog.onProbeResult(false))
        assertEquals(SessionWatchdog.Action.Continue, watchdog.onProbeResult(false))
        assertEquals(SessionWatchdog.Action.DeclareLost, watchdog.onProbeResult(false))
    }

    @Test
    fun repeatedCyclesDeclareLostEachTime() {
        val watchdog = SessionWatchdog(failureThreshold = 2)
        repeat(3) {
            assertEquals(SessionWatchdog.Action.Continue, watchdog.onProbeResult(false))
            assertEquals(SessionWatchdog.Action.DeclareLost, watchdog.onProbeResult(false))
        }
    }

    @Test
    fun resetClearsPendingFailures() {
        val watchdog = SessionWatchdog(failureThreshold = 2)
        assertEquals(SessionWatchdog.Action.Continue, watchdog.onProbeResult(false))
        watchdog.reset()
        assertEquals(SessionWatchdog.Action.Continue, watchdog.onProbeResult(false))
        assertEquals(SessionWatchdog.Action.DeclareLost, watchdog.onProbeResult(false))
    }

    @Test
    fun thresholdOfOneDeclaresLostImmediately() {
        val watchdog = SessionWatchdog(failureThreshold = 1)
        assertEquals(SessionWatchdog.Action.DeclareLost, watchdog.onProbeResult(false))
        assertEquals(SessionWatchdog.Action.DeclareLost, watchdog.onProbeResult(false))
    }

    @Test
    fun successReturnsContinue() {
        val watchdog = SessionWatchdog(failureThreshold = 1)
        assertEquals(SessionWatchdog.Action.Continue, watchdog.onProbeResult(true))
    }
}
