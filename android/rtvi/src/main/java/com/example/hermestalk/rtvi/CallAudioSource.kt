package com.example.hermestalk.rtvi

import android.content.Context
import android.util.Log
import java.io.IOException

/**
 * Where the app's outgoing microphone audio comes from.
 *
 * The production path is [DeviceMicAudioSource]: the Pipecat transport captures
 * the platform microphone through WebRTC's own audio device module, so this
 * source has nothing to feed. The emulator's microphone cannot be driven from a
 * test, so [FixtureAudioSource] reads 16 kHz mono PCM16 from a bundled asset and
 * hands it to the session frame by frame. In both cases the WebRTC transport and
 * the relay are real; only the origin of the samples differs.
 */
interface CallAudioSource {

    /** True when this source pushes frames itself instead of the device microphone. */
    val scripted: Boolean

    /** Sample rate of the frames this source produces. */
    val sampleRateHz: Int

    /** Length of one frame in milliseconds. */
    val frameMillis: Int

    /** Human-readable description for the trace and the logs. */
    fun describe(): String

    /**
     * Begin producing frames. Returns false when nothing was started (the
     * device microphone path, or an already-running source).
     */
    fun start(onFrame: (ByteArray) -> Unit): Boolean

    /** Stop producing frames and release the producer thread. */
    fun stop()
}

/** The device microphone: WebRTC captures it, so this source is a marker only. */
class DeviceMicAudioSource : CallAudioSource {
    override val scripted: Boolean = false
    override val sampleRateHz: Int = 16_000
    override val frameMillis: Int = 20

    override fun describe(): String = "device-mic"

    override fun start(onFrame: (ByteArray) -> Unit): Boolean = false

    override fun stop() = Unit
}

/**
 * Scripted microphone audio from a WAV asset: 16 kHz mono PCM16, emitted as
 * [frameMillis] frames paced in real time.
 *
 * The file must be a plain RIFF/WAVE PCM file; anything else fails loudly at
 * construction so a broken fixture cannot be mistaken for a silent call.
 */
class FixtureAudioSource(
    context: Context,
    assetPath: String,
    private val trace: CallTrace = CallTrace,
) : CallAudioSource {

    override val scripted: Boolean = true
    override val sampleRateHz: Int = SAMPLE_RATE_HZ
    override val frameMillis: Int = FRAME_MILLIS

    private val pcm: ByteArray = readPcm(context, assetPath)

    /**
     * Offset just past the last audible sample. The fixture carries a silence
     * tail so the relay's endpointing can fire; the trace marks the end of the
     * speech itself, which is the honest anchor for endpointing+STT latency.
     */
    private val speechBytes: Int = speechEndOffset(pcm)
    private val frameBytes: Int = SAMPLE_RATE_HZ * FRAME_MILLIS / 1000 * BYTES_PER_SAMPLE

    /** Frames the whole fixture is worth; a completed feed must reach this. */
    val totalFrames: Int = (pcm.size + frameBytes - 1) / frameBytes

    @Volatile
    private var running = false

    private var worker: Thread? = null

    /** Frames handed to the session since [start]; read by the test. */
    @Volatile
    var framesSent: Int = 0
        private set

    override fun describe(): String = "fixture(${pcm.size} bytes)"

    @Synchronized
    override fun start(onFrame: (ByteArray) -> Unit): Boolean {
        if (running) {
            return false
        }
        running = true
        framesSent = 0
        trace.record("audio.feed.start", mapOf("bytes" to pcm.size.toString()))
        val thread = Thread({ feed(onFrame) }, "hermestalk-fixture-audio")
        worker = thread
        thread.start()
        return true
    }

    @Synchronized
    override fun stop() {
        if (!running) {
            return
        }
        running = false
        worker?.interrupt()
        worker = null
    }

    private fun feed(onFrame: (ByteArray) -> Unit) {
        var offset = 0
        var nextAt = System.nanoTime()
        var speechMarked = false
        val framePeriodNanos = FRAME_MILLIS * 1_000_000L
        while (running && offset < pcm.size) {
            val end = minOf(offset + frameBytes, pcm.size)
            onFrame(pcm.copyOfRange(offset, end))
            offset = end
            framesSent += 1
            if (!speechMarked && end >= speechBytes) {
                speechMarked = true
                trace.record(
                    "audio.feed.speech.end",
                    mapOf("bytes" to speechBytes.toString(), "frames" to framesSent.toString()),
                )
            }
            nextAt += framePeriodNanos
            val waitNanos = nextAt - System.nanoTime()
            if (waitNanos > 0) {
                try {
                    Thread.sleep(waitNanos / 1_000_000L, (waitNanos % 1_000_000L).toInt())
                } catch (interrupted: InterruptedException) {
                    Thread.currentThread().interrupt()
                    break
                }
            }
        }
        if (running) {
            running = false
            trace.record(
                "audio.feed.end",
                mapOf("frames" to framesSent.toString(), "bytes" to pcm.size.toString()),
            )
        }
    }

    /**
     * Offset just past the last sample above [SILENCE_THRESHOLD]; the whole file
     * when it never goes quiet. Computed once so the trace can mark the end of
     * the spoken words rather than the end of the file's silence tail.
     */
    private fun speechEndOffset(pcm: ByteArray): Int {
        var last = -1
        var at = 0
        while (at + 1 < pcm.size) {
            val sample = (pcm[at].toInt() and 0xFF) or (pcm[at + 1].toInt() shl 8)
            if (kotlin.math.abs(sample) > SILENCE_THRESHOLD) {
                last = at
            }
            at += BYTES_PER_SAMPLE
        }
        return if (last < 0) pcm.size else last + BYTES_PER_SAMPLE
    }

    private fun readPcm(context: Context, assetPath: String): ByteArray {
        val bytes = try {
            context.assets.open(assetPath).use { it.readBytes() }
        } catch (error: IOException) {
            throw IllegalArgumentException("fixture asset $assetPath is missing", error)
        }
        require(bytes.size > HEADER_MIN_BYTES && String(bytes, 0, 4) == "RIFF") {
            "$assetPath is not a RIFF file"
        }
        require(String(bytes, 8, 4) == "WAVE") { "$assetPath is not a WAVE file" }

        var offset = 12
        var channels = 0
        var rate = 0
        var bits = 0
        var format = 0
        var dataOffset = -1
        var dataSize = 0
        while (offset + 8 <= bytes.size) {
            val id = String(bytes, offset, 4)
            val size = leInt(bytes, offset + 4)
            val body = offset + 8
            when (id) {
                "fmt " -> {
                    format = leShort(bytes, body)
                    channels = leShort(bytes, body + 2)
                    rate = leInt(bytes, body + 4)
                    bits = leShort(bytes, body + 14)
                }

                "data" -> {
                    dataOffset = body
                    dataSize = minOf(size, bytes.size - body)
                }
            }
            offset = body + size + (size and 1)
        }

        require(format == FORMAT_PCM) { "$assetPath must be PCM (got format $format)" }
        require(channels == 1) { "$assetPath must be mono (got $channels channels)" }
        require(rate == SAMPLE_RATE_HZ) { "$assetPath must be $SAMPLE_RATE_HZ Hz (got $rate)" }
        require(bits == 16) { "$assetPath must be 16-bit (got $bits)" }
        require(dataOffset >= 0 && dataSize > 0) { "$assetPath has no data chunk" }
        return bytes.copyOfRange(dataOffset, dataOffset + dataSize)
    }

    private fun leInt(bytes: ByteArray, at: Int): Int =
        (bytes[at].toInt() and 0xFF) or
            ((bytes[at + 1].toInt() and 0xFF) shl 8) or
            ((bytes[at + 2].toInt() and 0xFF) shl 16) or
            ((bytes[at + 3].toInt() and 0xFF) shl 24)

    private fun leShort(bytes: ByteArray, at: Int): Int =
        (bytes[at].toInt() and 0xFF) or ((bytes[at + 1].toInt() and 0xFF) shl 8)

    companion object {
        private const val TAG = "FixtureAudioSource"
        private const val SAMPLE_RATE_HZ = 16_000
        private const val FRAME_MILLIS = 20
        private const val BYTES_PER_SAMPLE = 2
        private const val FORMAT_PCM = 1
        private const val HEADER_MIN_BYTES = 44

        /** Peak amplitude below this counts as the silence tail, not speech. */
        private const val SILENCE_THRESHOLD = 300

        /** The relay's microphone bridge expects exactly this rate. */
        const val RELAY_SAMPLE_RATE_HZ = SAMPLE_RATE_HZ

        /** Logs the fixture that was loaded; the asset path is not a secret. */
        fun logLoaded(source: FixtureAudioSource) {
            Log.i(TAG, "scripted audio ${source.describe()}")
        }
    }
}

/**
 * Install point for the scripted source. The app uses the device microphone
 * unless a test installs a fixture before a call starts.
 */
object CallAudioSources {

    @Volatile
    var scripted: CallAudioSource? = null

    /** The source a new session should use: the installed fixture, else the device. */
    fun current(): CallAudioSource = scripted ?: DeviceMicAudioSource()

    /** Remove the installed fixture; later sessions use the device microphone. */
    fun clear() {
        scripted?.stop()
        scripted = null
    }
}
