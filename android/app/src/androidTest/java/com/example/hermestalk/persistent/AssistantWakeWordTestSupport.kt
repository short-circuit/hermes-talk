package com.example.hermestalk.persistent

import android.util.Log
import com.example.hermestalk.wakeword.WakeWordCapture
import com.example.hermestalk.wakeword.WakeWordCaptureStart
import com.example.hermestalk.wakeword.WakeWordConfig
import com.example.hermestalk.wakeword.WakeWordKeywordEngine
import com.example.hermestalk.wakeword.WavFixture
import com.example.hermestalk.wakeword.normalizeKeyword
import kotlin.math.sqrt

private const val TAG = "FixtureSequence"

/** A scripted source of microphone samples for the fixture-backed capture. */
interface WakeWordSampleSource {
    fun nextChunk(size: Int): FloatArray
}

/**
 * Shared playback cursor for the scripted microphone. The cursor deliberately
 * survives a capture restart: after a detection the runner stops capture, and
 * the next start continues from the next sample instead of replaying the
 * keyword from the beginning.
 */
class FixturePlayback(fixture: WavFixture) : WakeWordSampleSource {

    private val lock = Any()
    private val samples = fixture.samples.copyOf()
    private var cursor = 0

    init {
        require(samples.isNotEmpty()) { "the WAV fixture must contain samples" }
    }

    /** Next [size] samples, wrapping around the fixture when it reaches the end. */
    override fun nextChunk(size: Int): FloatArray {
        require(size > 0) { "chunk size must be positive" }
        return synchronized(lock) {
            val chunk = FloatArray(size)
            var written = 0
            while (written < size) {
                if (cursor >= samples.size) {
                    cursor = 0
                }
                val take = minOf(samples.size - cursor, size - written)
                samples.copyInto(chunk, written, cursor, cursor + take)
                cursor += take
                written += take
            }
            chunk
        }
    }
}

/**
 * Plays a fixed sequence of fixtures in the given order exactly once, inserting
 * [gapMillis] of silence between consecutive fixtures and returning silence
 * forever afterwards. Like [FixturePlayback] the cursor survives capture
 * restarts, so a detection does not replay the fixture that produced it.
 */
class FixtureSequencePlayback(
    fixtures: List<WavFixture>,
    private val gapMillis: Long = 3_000L,
) : WakeWordSampleSource {

    private val lock = Any()
    private val samples = fixtures.map { it.samples.copyOf() }
    private val gapSamples = (gapMillis * SAMPLES_PER_MILLISECOND).toInt()
    private var fixtureIndex = 0
    private var cursor = 0
    private var inGap = false

    /** True once the last fixture's samples have been handed out. */
    @Volatile
    var finished: Boolean = false
        private set

    init {
        require(fixtures.isNotEmpty()) { "the fixture sequence must not be empty" }
        require(gapMillis >= 0L) { "the gap must not be negative" }
        samples.forEach { fixture ->
            require(fixture.isNotEmpty()) { "the WAV fixture must contain samples" }
        }
    }

    override fun nextChunk(size: Int): FloatArray {
        require(size > 0) { "chunk size must be positive" }
        return synchronized(lock) {
            val chunk = FloatArray(size)
            var written = 0
            while (written < size && fixtureIndex < samples.size) {
                if (inGap) {
                    val take = minOf(gapSamples - cursor, size - written)
                    cursor += take
                    written += take
                    if (cursor >= gapSamples) {
                        inGap = false
                        cursor = 0
                        Log.d(TAG, "gap complete, fixture $fixtureIndex/${samples.size}")
                    }
                } else {
                    val current = samples[fixtureIndex]
                    val take = minOf(current.size - cursor, size - written)
                    current.copyInto(chunk, written, cursor, cursor + take)
                    cursor += take
                    written += take
                    if (cursor >= current.size) {
                        cursor = 0
                        fixtureIndex += 1
                        Log.d(TAG, "fixture $fixtureIndex/${samples.size} complete")
                        inGap = fixtureIndex < samples.size && gapSamples > 0
                    }
                }
            }
            if (fixtureIndex >= samples.size) {
                finished = true
            }
            chunk
        }
    }

    private companion object {
        const val SAMPLES_PER_MILLISECOND = 16L
    }
}

/**
 * A deterministic replacement for [WakeWordCapture] used by the instrumentation
 * service tests. It feeds the shared fixture through the real on-device keyword
 * engine at roughly real time (100 ms chunks) and reports a keyword only when
 * the engine returns the expected normalized phrase.
 */
class FixtureWakeWordCapture(
    private val engine: WakeWordKeywordEngine,
    private val config: WakeWordConfig,
    private val source: WakeWordSampleSource,
    private val expectedKeyword: String = "HEYHERMES",
    private val chunkDelayMillis: Long = 100L,
) : WakeWordCapture {

    override var onKeyword: ((String) -> Unit)? = null
    override var onLevel: ((Float) -> Unit)? = null
    override var onFailure: ((String) -> Unit)? = null

    private val lock = Any()

    @Volatile
    private var capturing = false

    @Volatile
    private var thread: Thread? = null

    @Volatile
    private var recordCreated = false

    @Volatile
    private var recordReleased = true

    override val running: Boolean
        get() = capturing

    override val threadAlive: Boolean
        get() = thread?.isAlive == true

    override val audioRecordCreated: Boolean
        get() = recordCreated

    override val audioRecordReleased: Boolean
        get() = recordReleased

    override fun start(): WakeWordCaptureStart {
        synchronized(lock) {
            if (capturing) {
                return WakeWordCaptureStart.Started
            }
            recordCreated = true
            recordReleased = false
            try {
                engine.newStream()
            } catch (error: Exception) {
                recordReleased = true
                return WakeWordCaptureStart.Failed(
                    error.message?.takeIf { it.isNotBlank() }
                        ?: error.javaClass.simpleName,
                )
            }
            capturing = true
            thread = Thread(
                { captureLoop() },
                "fixture-wakeword-capture",
            ).also { it.start() }
            Log.d(TAG, "capture started")
        }
        return WakeWordCaptureStart.Started
    }

    override fun stop() {
        synchronized(lock) {
            capturing = false
        }
        val current = thread
        if (current != null && current !== Thread.currentThread()) {
            try {
                current.join(STOP_JOIN_TIMEOUT_MILLIS)
            } catch (_: InterruptedException) {
                Thread.currentThread().interrupt()
            }
            if (current.isAlive) {
                current.interrupt()
            }
        }
        synchronized(lock) {
            thread = null
            recordReleased = true
        }
        Log.d(TAG, "capture stopped")
    }

    private fun captureLoop() {
        try {
            while (capturing) {
                val chunk = source.nextChunk(config.chunkSamples)
                var sumSquares = 0.0
                for (sample in chunk) {
                    sumSquares += sample.toDouble() * sample
                }
                onLevel?.invoke(
                    sqrt(sumSquares / chunk.size).toFloat().coerceIn(0f, 1f),
                )
                engine.accept(chunk, config.captureSampleRate)
                val keyword = engine.poll()
                if (keyword != null) {
                    Log.d(TAG, "keyword=$keyword normalized=${normalizeKeyword(keyword)}")
                }
                if (
                    keyword != null &&
                    normalizeKeyword(keyword) == expectedKeyword
                ) {
                    onKeyword?.invoke(keyword)
                }
                try {
                    Thread.sleep(chunkDelayMillis)
                } catch (_: InterruptedException) {
                    break
                }
            }
        } catch (error: Exception) {
            if (capturing) {
                onFailure?.invoke(
                    error.message?.takeIf { it.isNotBlank() }
                        ?: error.javaClass.simpleName,
                )
            }
        } finally {
            synchronized(lock) {
                capturing = false
            }
        }
    }

    private companion object {
        const val STOP_JOIN_TIMEOUT_MILLIS = 2_000L
    }
}
