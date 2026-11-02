package com.example.hermestalk.call

/**
 * Bounded, envelope-smoothed microphone level history for the call visualiser.
 * The meter never grows without limit and treats non-finite input as silence.
 */
class CallLevelMeter(
    private val capacity: Int = 48,
    private val attack: Float = 0.65f,
    private val decay: Float = 0.18f,
) {

    private val size = capacity.coerceAtLeast(1)
    private val attackFactor = attack.coerceIn(0f, 1f)
    private val decayFactor = decay.coerceIn(0f, 1f)
    private val history = ArrayDeque<Float>(size)

    init {
        repeat(size) { history.addLast(0f) }
    }

    /** Push [level], clamp it to 0..1 and return the bounded history. */
    fun push(level: Float): List<Float> {
        val target = sanitize(level)
        val previous = history.lastOrNull() ?: 0f
        val factor = if (target > previous) attackFactor else decayFactor
        val smoothed = (previous + (target - previous) * factor).coerceIn(0f, 1f)
        history.removeFirst()
        history.addLast(smoothed)
        return history.toList()
    }

    /** Reset the history to silence and return it. */
    fun clear(): List<Float> {
        history.clear()
        repeat(size) { history.addLast(0f) }
        return history.toList()
    }

    /** Most recent smoothed level. */
    fun current(): Float = history.lastOrNull() ?: 0f

    private fun sanitize(level: Float): Float {
        if (level.isNaN() || level.isInfinite()) {
            return 0f
        }
        return level.coerceIn(0f, 1f)
    }
}
