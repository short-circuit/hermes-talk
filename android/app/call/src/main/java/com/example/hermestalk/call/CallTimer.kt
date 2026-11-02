package com.example.hermestalk.call

import java.util.Locale

/** Formats call durations as mm:ss, with h:mm:ss once an hour has elapsed. */
object CallTimer {

    /** Format [seconds] as a call duration. Negative values are treated as zero. */
    fun format(seconds: Long): String {
        val safe = seconds.coerceAtLeast(0L)
        val hours = safe / 3600L
        val minutes = (safe % 3600L) / 60L
        val secs = safe % 60L
        return if (hours > 0L) {
            String.format(Locale.US, "%d:%02d:%02d", hours, minutes, secs)
        } else {
            String.format(Locale.US, "%02d:%02d", minutes, secs)
        }
    }
}
