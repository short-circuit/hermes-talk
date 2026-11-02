package com.example.hermestalk.wakeword

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import androidx.core.content.ContextCompat
import kotlin.math.sqrt

/** Injected microphone-permission check. */
fun interface WakeWordPermissionChecker {
    fun hasPermission(): Boolean
}

/** Result of attempting to start capture. */
sealed interface WakeWordCaptureStart {
    data object Started : WakeWordCaptureStart
    data object PermissionDenied : WakeWordCaptureStart
    data class Failed(val reason: String) : WakeWordCaptureStart
}

/** Capture seam used by the controller and the instrumentation tests. */
interface WakeWordCapture {
    var onKeyword: ((String) -> Unit)?
    var onLevel: ((Float) -> Unit)?
    var onFailure: ((String) -> Unit)?

    fun start(): WakeWordCaptureStart
    fun stop()

    val running: Boolean
    val threadAlive: Boolean
    val audioRecordCreated: Boolean
    val audioRecordReleased: Boolean
}

/**
 * AudioRecord capture loop on a dedicated thread. It records 16 kHz mono PCM16
 * from VOICE_RECOGNITION, reads 100 ms chunks, converts them to float [-1,1]
 * and feeds the keyword engine. A denied permission returns
 * [WakeWordCaptureStart.PermissionDenied] without creating an AudioRecord.
 */
class WakeWordListener(
    private val engine: WakeWordKeywordEngine,
    private val permissionChecker: WakeWordPermissionChecker,
    private val config: WakeWordConfig,
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
    private var audioRecord: AudioRecord? = null

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
        if (!permissionChecker.hasPermission()) {
            return WakeWordCaptureStart.PermissionDenied
        }
        synchronized(lock) {
            if (capturing) {
                return WakeWordCaptureStart.Started
            }
            val minBuffer = AudioRecord.getMinBufferSize(
                SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
            )
            if (minBuffer <= 0) {
                return fail("AudioRecord is unavailable on this device")
            }
            val bufferBytes = maxOf(
                minBuffer * 2,
                config.chunkSamples * BYTES_PER_SAMPLE * 2,
            )
            val record = try {
                AudioRecord(
                    MediaRecorder.AudioSource.VOICE_RECOGNITION,
                    SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT,
                    bufferBytes,
                )
            } catch (error: Exception) {
                return fail("Could not create AudioRecord: ${reason(error)}")
            }
            recordCreated = true
            recordReleased = false
            if (record.state != AudioRecord.STATE_INITIALIZED) {
                releaseRecord(record)
                return fail("AudioRecord did not initialize")
            }

            audioRecord = record
            try {
                engine.newStream()
            } catch (error: Exception) {
                releaseRecord(record)
                audioRecord = null
                return fail("Could not create the keyword stream: ${reason(error)}")
            }

            capturing = true
            thread = Thread(
                { captureLoop(record) },
                "hermes-wakeword-capture",
            ).also { it.start() }
            return WakeWordCaptureStart.Started
        }
    }

    override fun stop() {
        val record: AudioRecord?
        val captureThread: Thread?
        synchronized(lock) {
            capturing = false
            record = audioRecord
            captureThread = thread
            audioRecord = null
            thread = null
        }
        if (record != null) {
            try {
                if (record.recordingState == AudioRecord.RECORDSTATE_RECORDING) {
                    record.stop()
                }
            } catch (_: Exception) {
                // The capture loop may already have stopped it.
            }
            releaseRecord(record)
        }
        captureThread?.join(STOP_JOIN_TIMEOUT_MILLIS)
        if (captureThread?.isAlive == true) {
            captureThread.interrupt()
        }
    }

    private fun captureLoop(record: AudioRecord) {
        val shortBuffer = ShortArray(config.chunkSamples)
        val floatBuffer = FloatArray(config.chunkSamples)
        try {
            record.startRecording()
            if (record.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                fail("AudioRecord did not start recording")
                return
            }
            while (capturing) {
                val read = record.read(shortBuffer, 0, shortBuffer.size)
                if (read > 0) {
                    var sumSquares = 0.0
                    for (index in 0 until read) {
                        val sample = shortBuffer[index] / 32768.0f
                        floatBuffer[index] = sample
                        sumSquares += sample.toDouble() * sample
                    }
                    val level = sqrt(sumSquares / read).toFloat().coerceIn(0f, 1f)
                    onLevel?.invoke(level)
                    engine.accept(floatBuffer.copyOf(read), SAMPLE_RATE)
                    engine.poll()?.let { keyword -> onKeyword?.invoke(keyword) }
                } else if (read < 0) {
                    fail("AudioRecord read failed with code $read")
                    return
                }
            }
        } catch (error: Exception) {
            if (capturing) {
                fail("Capture failed: ${reason(error)}")
            }
        } finally {
            releaseRecord(record)
        }
    }

    private fun fail(reason: String): WakeWordCaptureStart.Failed {
        capturing = false
        onFailure?.invoke(reason)
        return WakeWordCaptureStart.Failed(reason)
    }

    private fun releaseRecord(record: AudioRecord) {
        synchronized(lock) {
            if (recordReleased) {
                return
            }
            recordReleased = true
        }
        try {
            record.release()
        } catch (_: Exception) {
            // Idempotent release.
        }
    }

    private fun reason(error: Throwable): String =
        error.message?.takeIf { it.isNotBlank() } ?: error.javaClass.simpleName

    companion object {
        const val SAMPLE_RATE = 16_000
        private const val BYTES_PER_SAMPLE = 2
        private const val STOP_JOIN_TIMEOUT_MILLIS = 2_000L

        /** Production checker for the RECORD_AUDIO runtime permission. */
        fun permissionChecker(context: Context): WakeWordPermissionChecker =
            WakeWordPermissionChecker {
                ContextCompat.checkSelfPermission(
                    context,
                    Manifest.permission.RECORD_AUDIO,
                ) == PackageManager.PERMISSION_GRANTED
            }
    }
}
