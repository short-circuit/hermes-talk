package com.example.hermestalk.wakeword

/**
 * Pure cooldown gate for detections. The caller supplies the clock, so the
 * gate has no Android or wall-clock dependency. It suppresses repeated hits
 * inside the cooldown window and resets when listening stops.
 */
class DetectionGate(private val cooldownMillis: Long) {

    private var lastAcceptedMillis: Long? = null

    /** True when a detection at [nowMillis] is outside the cooldown window. */
    @Synchronized
    fun shouldAccept(nowMillis: Long): Boolean {
        val last = lastAcceptedMillis ?: return true
        return nowMillis - last >= cooldownMillis
    }

    /** Record an accepted detection at [nowMillis]. */
    @Synchronized
    fun record(nowMillis: Long) {
        lastAcceptedMillis = nowMillis
    }

    /** Clear the cooldown, for example when listening stops. */
    @Synchronized
    fun reset() {
        lastAcceptedMillis = null
    }
}
