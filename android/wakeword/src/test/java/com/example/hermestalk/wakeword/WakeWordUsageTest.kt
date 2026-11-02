package com.example.hermestalk.wakeword

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.nio.charset.StandardCharsets

class WakeWordUsageTest {

    private lateinit var directory: File

    @Before
    fun setUp() {
        directory = File(
            System.getProperty("java.io.tmpdir"),
            "wakeword-usage-${System.nanoTime()}",
        )
        directory.mkdirs()
    }

    @After
    fun tearDown() {
        directory.deleteRecursively()
    }

    @Test
    fun arithmeticUsesLinuxClockTicks() {
        assertEquals(60_000L, WakeWordUsageMath.listeningMillis(1_000L, 61_000L))
        assertEquals(0L, WakeWordUsageMath.listeningMillis(61_000L, 1_000L))
        assertEquals(10_000L, WakeWordUsageMath.cpuMillisFromTickDelta(1_000L))
        assertEquals(0L, WakeWordUsageMath.cpuMillisFromTickDelta(-5L))
    }

    @Test
    fun snapshotAccumulatesWallAndCpuDeltas() {
        var now = 1_000L
        var ticks = 100L
        val usage = WakeWordUsage(
            clock = { now },
            cpuTicksReader = { ticks },
        )
        usage.startListening()
        usage.recordDetection()
        now = 61_000L
        ticks = 1_100L
        usage.stopListening()

        val snapshot = usage.snapshot()
        assertEquals(60_000L, snapshot.listeningWallMillis)
        assertEquals(10_000L, snapshot.cpuMillis)
        assertEquals(1, snapshot.detectionCount)
    }

    @Test
    fun unreadableCpuFallsBackToZero() {
        var now = 1_000L
        val usage = WakeWordUsage(
            clock = { now },
            cpuTicksReader = { null },
        )
        usage.startListening()
        now = 11_000L
        usage.stopListening()
        val snapshot = usage.snapshot()
        assertEquals(10_000L, snapshot.listeningWallMillis)
        assertEquals(0L, snapshot.cpuMillis)
    }

    @Test
    fun activeWindowIsIncludedInSnapshot() {
        var now = 1_000L
        var ticks = 50L
        val usage = WakeWordUsage(
            clock = { now },
            cpuTicksReader = { ticks },
        )
        usage.startListening()
        now = 6_000L
        ticks = 60L
        val snapshot = usage.snapshot()
        assertEquals(5_000L, snapshot.listeningWallMillis)
        assertEquals(100L, snapshot.cpuMillis)
    }

    @Test
    fun writeSummaryProducesJsonWithoutPaths() {
        var now = 1_000L
        var ticks = 100L
        val usage = WakeWordUsage(
            clock = { now },
            cpuTicksReader = { ticks },
        )
        usage.startListening()
        usage.recordDetection()
        now = 61_000L
        ticks = 1_100L
        usage.stopListening()

        val snapshot = usage.writeSummary(directory)
        val file = File(directory, "usage.json")
        assertTrue(file.isFile)
        val text = file.readText(StandardCharsets.UTF_8)
        assertEquals(60_000L, snapshot.listeningWallMillis)
        assertEquals(10_000L, snapshot.cpuMillis)
        assertTrue(text.contains("\"listening_wall_ms\":60000"))
        assertTrue(text.contains("\"cpu_ms\":10000"))
        assertTrue(text.contains("\"detection_count\":1"))
        assertTrue(!text.contains(directory.absolutePath))
    }
}
