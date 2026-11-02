package com.example.hermestalk.wakeword

import java.io.File
import java.nio.charset.StandardCharsets
import java.util.Locale

/** One battery and detection telemetry snapshot. */
data class WakeWordUsageSnapshot(
    val listeningWallMillis: Long,
    val cpuMillis: Long,
    val detectionCount: Int,
)

/** Pure arithmetic helpers for the usage telemetry. */
object WakeWordUsageMath {
    /** Linux USER_HZ assumed by `/proc/self/stat`; Android uses 100. */
    const val CLOCK_TICKS_PER_SECOND = 100L

    fun listeningMillis(startMillis: Long, endMillis: Long): Long =
        (endMillis - startMillis).coerceAtLeast(0L)

    fun cpuMillisFromTickDelta(deltaTicks: Long): Long =
        (deltaTicks.coerceAtLeast(0L) * 1_000L) / CLOCK_TICKS_PER_SECOND
}

/**
 * Battery telemetry for continuous listening: wall-clock listening time,
 * process CPU time read from `/proc/self/stat`, and the detection count.
 * The CPU reader is injectable so the arithmetic can be tested on the JVM.
 */
class WakeWordUsage(
    private val clock: () -> Long = System::currentTimeMillis,
    private val cpuTicksReader: () -> Long? = { readProcessCpuTicksFromProc() },
) {
    private var accumulatedListeningMillis = 0L
    private var accumulatedCpuMillis = 0L
    private var listeningStartedAt: Long? = null
    private var cpuStartTicks: Long? = null
    private var detections = 0

    /** Start a listening window; repeated calls while active are ignored. */
    @Synchronized
    fun startListening() {
        if (listeningStartedAt != null) {
            return
        }
        listeningStartedAt = clock()
        cpuStartTicks = cpuTicksReader()
    }

    /** Close the active listening window and accumulate its wall and CPU time. */
    @Synchronized
    fun stopListening() {
        val startedAt = listeningStartedAt ?: return
        accumulatedListeningMillis += WakeWordUsageMath.listeningMillis(startedAt, clock())
        val startTicks = cpuStartTicks
        val endTicks = cpuTicksReader()
        if (startTicks != null && endTicks != null) {
            accumulatedCpuMillis += WakeWordUsageMath.cpuMillisFromTickDelta(endTicks - startTicks)
        }
        listeningStartedAt = null
        cpuStartTicks = null
    }

    /** Count one accepted detection. */
    @Synchronized
    fun recordDetection() {
        detections += 1
    }

    /** Current telemetry, including an active listening window. */
    @Synchronized
    fun snapshot(): WakeWordUsageSnapshot {
        val activeWall = listeningStartedAt?.let {
            WakeWordUsageMath.listeningMillis(it, clock())
        } ?: 0L
        val activeCpu = cpuStartTicks?.let { start ->
            cpuTicksReader()?.let { end ->
                WakeWordUsageMath.cpuMillisFromTickDelta(end - start)
            }
        } ?: 0L
        return WakeWordUsageSnapshot(
            listeningWallMillis = accumulatedListeningMillis + activeWall,
            cpuMillis = accumulatedCpuMillis + activeCpu,
            detectionCount = detections,
        )
    }

    /** Write `usage.json` in [directory] and return the snapshot that was written. */
    @Synchronized
    fun writeSummary(directory: File): WakeWordUsageSnapshot {
        val snapshot = snapshot()
        directory.mkdirs()
        val json = String.format(
            Locale.US,
            "{\"listening_wall_ms\":%d,\"cpu_ms\":%d,\"detection_count\":%d}\n",
            snapshot.listeningWallMillis,
            snapshot.cpuMillis,
            snapshot.detectionCount,
        )
        File(directory, "usage.json").writeText(json, StandardCharsets.UTF_8)
        return snapshot
    }

}

/** Read utime+stime from `/proc/self/stat`; returns null when unreadable. */
private fun readProcessCpuTicksFromProc(): Long? = try {
    val text = File("/proc/self/stat").readText(StandardCharsets.UTF_8)
    val close = text.lastIndexOf(')')
    if (close < 0) {
        null
    } else {
        val fields = text.substring(close + 2).trim().split(Regex("\\s+"))
        if (fields.size < 13) {
            null
        } else {
            val utime = fields[11].toLongOrNull()
            val stime = fields[12].toLongOrNull()
            if (utime == null || stime == null) null else utime + stime
        }
    }
} catch (_: Exception) {
    null
}
