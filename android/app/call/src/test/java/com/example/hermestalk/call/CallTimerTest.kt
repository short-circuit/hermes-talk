package com.example.hermestalk.call

import org.junit.Assert.assertEquals
import org.junit.Test

class CallTimerTest {

    @Test
    fun formatsSecondsAndMinutes() {
        assertEquals("00:00", CallTimer.format(0))
        assertEquals("00:05", CallTimer.format(5))
        assertEquals("01:05", CallTimer.format(65))
        assertEquals("59:59", CallTimer.format(3599))
    }

    @Test
    fun formatsHoursWithSingleHourDigit() {
        assertEquals("1:00:00", CallTimer.format(3600))
        assertEquals("2:03:04", CallTimer.format(7384))
    }

    @Test
    fun negativeDurationsClampToZero() {
        assertEquals("00:00", CallTimer.format(-10))
    }
}
