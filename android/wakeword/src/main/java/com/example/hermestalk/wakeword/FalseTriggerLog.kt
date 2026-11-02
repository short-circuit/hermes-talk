package com.example.hermestalk.wakeword

import java.io.File
import java.io.FileOutputStream
import java.nio.charset.StandardCharsets
import java.util.Locale

/** The trigger source recorded in the false-trigger log. */
enum class WakeWordSource(val wireName: String) {
    WakeWord("wake_word"),
    PushToTalk("push_to_talk"),
    ;

    companion object {
        fun fromWireName(value: String): WakeWordSource? =
            entries.firstOrNull { it.wireName == value }
    }
}

/** One JSON-lines entry in the false-trigger log. */
data class FalseTriggerEntry(
    val timestampMillis: Long,
    val keyword: String,
    val source: WakeWordSource,
    val threshold: Float,
    val listeningMillis: Long,
)

/**
 * Append-only JSON-lines log of detections in a caller-supplied directory. The
 * log is capped at [maxBytes]; on overflow the current file becomes
 * `events.log.1` (one rotated backup) and a fresh file is started. Entries
 * contain only timestamps, the keyword, the source, the threshold and a
 * duration, so no paths or personal data are written.
 */
class FalseTriggerLog(
    private val directory: File,
    maxBytes: Long = 65_536L,
) {
    private val logFile = File(directory, "events.log")
    private val backupFile = File(directory, "events.log.1")

    @Volatile
    private var byteBudget = maxBytes

    /** Update the byte budget used by subsequent appends. */
    @Synchronized
    fun updateCap(maxBytes: Long) {
        byteBudget = maxBytes.coerceAtLeast(1L)
    }

    /** Append [entry] and return true when it was written. */
    @Synchronized
    fun append(entry: FalseTriggerEntry): Boolean {
        return try {
            directory.mkdirs()
            val line = format(entry).toByteArray(StandardCharsets.UTF_8)
            val cap = byteBudget
            if (logFile.isFile && logFile.length() + line.size > cap) {
                rotate()
            }
            FileOutputStream(logFile, true).use { output ->
                if (line.size > cap) {
                    output.write(line, 0, cap.toInt())
                } else {
                    output.write(line)
                }
            }
            true
        } catch (_: Exception) {
            false
        }
    }

    /** Read the most recent [count] entries, tolerating a partial last line. */
    @Synchronized
    fun readRecent(count: Int): List<FalseTriggerEntry> {
        if (count <= 0) {
            return emptyList()
        }
        val entries = mutableListOf<FalseTriggerEntry>()
        for (file in listOf(backupFile, logFile)) {
            if (!file.isFile) {
                continue
            }
            file.bufferedReader(StandardCharsets.UTF_8).useLines { lines ->
                lines.forEach { line ->
                    parse(line)?.let { entries += it }
                }
            }
        }
        return entries.sortedBy { it.timestampMillis }.takeLast(count)
    }

    /** Delete both the current and rotated files. */
    @Synchronized
    fun clear() {
        logFile.delete()
        backupFile.delete()
    }

    private fun rotate() {
        if (backupFile.exists()) {
            backupFile.delete()
        }
        if (logFile.exists() && !logFile.renameTo(backupFile)) {
            logFile.copyTo(backupFile, overwrite = true)
            logFile.delete()
        }
    }

    private fun format(entry: FalseTriggerEntry): String {
        val threshold = if (entry.threshold.isFinite()) entry.threshold else 0.0f
        return String.format(
            Locale.US,
            "{\"ts\":%d,\"keyword\":\"%s\",\"source\":\"%s\",\"threshold\":%s,\"listening_ms\":%d}\n",
            entry.timestampMillis,
            escape(entry.keyword),
            entry.source.wireName,
            threshold.toString(),
            entry.listeningMillis.coerceAtLeast(0L),
        )
    }

    private fun parse(line: String): FalseTriggerEntry? {
        val match = LINE.matchEntire(line.trim()) ?: return null
        val source = WakeWordSource.fromWireName(match.groupValues[3]) ?: return null
        val threshold = match.groupValues[4].toFloatOrNull() ?: return null
        val timestamp = match.groupValues[1].toLongOrNull() ?: return null
        val listeningMillis = match.groupValues[5].toLongOrNull() ?: return null
        return FalseTriggerEntry(
            timestampMillis = timestamp,
            keyword = unescape(match.groupValues[2]),
            source = source,
            threshold = threshold,
            listeningMillis = listeningMillis,
        )
    }

    private fun escape(value: String): String = buildString {
        for (character in value) {
            when (character) {
                '"' -> append("\\\"")
                '\\' -> append("\\\\")
                '\n' -> append("\\n")
                '\r' -> append("\\r")
                '\t' -> append("\\t")
                else -> if (character.code < 0x20) {
                    append(String.format(Locale.US, "\\u%04x", character.code))
                } else {
                    append(character)
                }
            }
        }
    }

    private fun unescape(value: String): String = buildString {
        var index = 0
        while (index < value.length) {
            val character = value[index]
            if (character == '\\' && index + 1 < value.length) {
                when (val next = value[index + 1]) {
                    '"' -> append('"')
                    '\\' -> append('\\')
                    'n' -> append('\n')
                    'r' -> append('\r')
                    't' -> append('\t')
                    'u' -> {
                        if (index + 5 < value.length) {
                            val code = value.substring(index + 2, index + 6).toIntOrNull(16)
                            if (code != null) {
                                append(code.toChar())
                                index += 4
                            }
                        }
                    }

                    else -> append(next)
                }
                index += 2
            } else {
                append(character)
                index += 1
            }
        }
    }

    private companion object {
        val LINE = Regex(
            """^\{"ts":(\d+),"keyword":"((?:\\.|[^"\\])*)","source":"([^"]*)","threshold":([-+0-9.eE]+),"listening_ms":(\d+)\}$""",
        )
    }
}
