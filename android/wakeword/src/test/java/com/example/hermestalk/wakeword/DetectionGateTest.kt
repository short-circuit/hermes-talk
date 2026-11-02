package com.example.hermestalk.wakeword

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DetectionGateTest {

    @Test
    fun firstDetectionIsAccepted() {
        val gate = DetectionGate(2500L)
        assertTrue(gate.shouldAccept(1_000L))
    }

    @Test
    fun repeatsInsideCooldownAreSuppressed() {
        val gate = DetectionGate(2500L)
        gate.record(1_000L)
        assertFalse(gate.shouldAccept(1_001L))
        assertFalse(gate.shouldAccept(3_499L))
        assertTrue(gate.shouldAccept(3_500L))
    }

    @Test
    fun resetClearsTheWindow() {
        val gate = DetectionGate(2500L)
        gate.record(1_000L)
        assertFalse(gate.shouldAccept(1_100L))
        gate.reset()
        assertTrue(gate.shouldAccept(1_100L))
    }

    @Test
    fun zeroCooldownAcceptsEveryDetection() {
        val gate = DetectionGate(0L)
        gate.record(1_000L)
        assertTrue(gate.shouldAccept(1_000L))
    }

    @Test
    fun clockGoingBackwardsDoesNotAccept() {
        val gate = DetectionGate(2500L)
        gate.record(5_000L)
        assertFalse(gate.shouldAccept(4_000L))
    }
}
