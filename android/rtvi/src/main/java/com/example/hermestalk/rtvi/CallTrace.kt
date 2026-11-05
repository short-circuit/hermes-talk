package com.example.hermestalk.rtvi

/**
 * Timestamped record of the call events the app observed.
 *
 * The connection state machine, the Pipecat callbacks, and the scripted audio
 * source write here, so an instrumentation test can assert on what the app
 * itself saw and measure the gaps between those observations. It carries no
 * audio and no token, only event names, monotonic timestamps, and short
 * non-sensitive details.
 */
object CallTrace {

    /** One observed event. [atNanos] is a [System.nanoTime] reading. */
    data class Entry(
        val name: String,
        val atNanos: Long,
        val detail: Map<String, String> = emptyMap(),
    ) {
        /** Milliseconds from [previous] to this entry; negative when out of order. */
        fun millisSince(previous: Entry): Long = (atNanos - previous.atNanos) / 1_000_000L
    }

    private val lock = Any()
    private val entries = mutableListOf<Entry>()

    /** Append one event. Safe from any thread. Keeps only the most recent events. */
    fun record(name: String, detail: Map<String, String> = emptyMap()) {
        val entry = Entry(name, System.nanoTime(), detail)
        synchronized(lock) {
            entries += entry
            if (entries.size > MAX_ENTRIES) {
                entries.removeAt(0)
            }
        }
    }

    /** A stable copy of everything recorded so far. */
    fun snapshot(): List<Entry> = synchronized(lock) { entries.toList() }

    /** Drop everything; call before starting a scenario. */
    fun reset() {
        synchronized(lock) { entries.clear() }
    }

    /** First entry with [name], or null. */
    fun first(name: String): Entry? = snapshot().firstOrNull { it.name == name }

    /** Last entry with [name], or null. */
    fun last(name: String): Entry? = snapshot().lastOrNull { it.name == name }

    /** Cap so a long call cannot grow the trace without bound. */
    private const val MAX_ENTRIES = 512
}
