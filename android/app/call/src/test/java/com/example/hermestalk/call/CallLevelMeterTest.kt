package com.example.hermestalk.call

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class CallLevelMeterTest {

    @Test
    fun historyIsAlwaysBoundedToCapacity() {
        val meter = CallLevelMeter(capacity = 8)
        repeat(20) { index ->
            val levels = meter.push(index / 19f)
            assertEquals(8, levels.size)
        }
    }

    @Test
    fun nanAndInfinityAreTreatedAsSilence() {
        val meter = CallLevelMeter(capacity = 4)
        meter.push(1f)
        val afterNan = meter.push(Float.NaN)
        assertEquals(4, afterNan.size)
        assertTrue(afterNan.all { it.isFinite() && it in 0f..1f })

        val afterInfinity = meter.push(Float.POSITIVE_INFINITY)
        assertTrue(afterInfinity.all { it.isFinite() && it in 0f..1f })
        assertTrue(meter.current() in 0f..1f)
    }

    @Test
    fun attackAndDecayStayInsideBounds() {
        val meter = CallLevelMeter(capacity = 4, attack = 0.65f, decay = 0.18f)
        val attacked = meter.push(1f)
        val peak = attacked.last()
        assertTrue(peak in 0f..1f)
        assertTrue(peak > 0f)

        val decayed = meter.push(0f)
        assertTrue(decayed.last() in 0f..1f)
        assertTrue(decayed.last() < peak)
    }

    @Test
    fun clearResetsHistoryToSilence() {
        val meter = CallLevelMeter(capacity = 5)
        meter.push(0.8f)
        val cleared = meter.clear()
        assertEquals(5, cleared.size)
        assertTrue(cleared.all { it == 0f })
        assertEquals(0f, meter.current(), 0f)
    }

    @Test
    fun zeroCapacityIsClampedToOneSample() {
        val meter = CallLevelMeter(capacity = 0)
        val levels = meter.push(0.5f)
        assertEquals(1, levels.size)
    }
}
