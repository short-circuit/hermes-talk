package com.example.hermestalk.spike

import android.annotation.SuppressLint
import android.content.Context
import android.media.audiofx.AcousticEchoCanceler
import android.media.AudioAttributes
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.media.audiofx.NoiseSuppressor
import android.os.Build
import android.os.PowerManager
import android.util.Log
import java.util.concurrent.LinkedBlockingDeque
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import kotlin.math.PI
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * Owns the on-device audio path: capture, render, routing, wake lock, and the transport.
 *
 * Everything here is what a real client has to do anyway; the transport is injected so the spike can
 * run against a stub relay while the RTVI/LiveKit decision is being made.
 */
class AudioEngine(
    private val context: Context,
    private val host: String,
    private val port: Int,
    private val upstreamToneHz: Double = 0.0,
    private val mediaUsage: Boolean = false,
) {
    private val running = AtomicBoolean(false)
    private val upFrames = AtomicLong()
    private val downFrames = AtomicLong()
    private val upBytes = AtomicLong()
    private val downBytes = AtomicLong()

    @Volatile private var upRms = 0.0
    @Volatile private var downRms = 0.0
    @Volatile private var captureState = "idle"
    @Volatile private var playbackState = "idle"
    @Volatile private var routeInfo = "unset"
    @Volatile private var tonePhase = 0.0
    @Volatile private var droppedFrames = 0L

    private val playQueue = LinkedBlockingDeque<ByteArray>(PLAY_QUEUE_FRAMES)

    private var record: AudioRecord? = null
    private var track: AudioTrack? = null
    private var aec: AcousticEchoCanceler? = null
    private var ns: NoiseSuppressor? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var audioManager: AudioManager? = null
    private var transport: Transport? = null
    private var deviceCallback: AudioDeviceCallback? = null
    private val threads = mutableListOf<Thread>()

    fun start() {
        if (running.getAndSet(true)) return
        Log.i(TAG, "engine: start host=$host port=$port upstream_tone_hz=$upstreamToneHz")
        acquireWakeLock()
        audioManager = context.getSystemService(Context.AUDIO_SERVICE) as? AudioManager
        configureMode()
        if (upstreamToneHz > 0.0) {
            captureState = "generated-tone"
            startGeneratedUpstream(upstreamToneHz)
        } else {
            startCapture()
        }
        startPlayback()
        startWriter()
        selectCommunicationDevice()
        registerDeviceCallback()
        transport = StubRelayTransport(host, port) { onDownstream(it) }
        transport?.connect()
        startStats()
    }

    fun stop() {
        if (!running.getAndSet(false)) return
        Log.i(TAG, "engine: stopping")
        transport?.close()
        transport = null
        record?.let {
            try {
                it.stop()
            } catch (_: Throwable) {
            }
            it.release()
        }
        record = null
        aec?.release()
        aec = null
        ns?.release()
        ns = null
        track?.let {
            try {
                it.stop()
            } catch (_: Throwable) {
            }
            it.release()
        }
        track = null
        unregisterDeviceCallback()
        audioManager?.let { am ->
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) am.clearCommunicationDevice()
            try {
                am.mode = AudioManager.MODE_NORMAL
            } catch (_: Throwable) {
            }
        }
        wakeLock?.let {
            if (it.isHeld) it.release()
        }
        wakeLock = null
        threads.forEach { it.join(500) }
        threads.clear()
        Log.i(TAG, "engine: stopped up_frames=${upFrames.get()} down_frames=${downFrames.get()}")
    }

    /**
     * Downstream frames are counted on arrival and queued for rendering. A full queue drops the oldest
     * frame instead of blocking the socket read, so the transport stays measurable even when the audio
     * HAL refuses to pull (which is what happens on this TV box once HDMI disconnects).
     */
    private fun onDownstream(frame: ByteArray) {
        downFrames.incrementAndGet()
        downBytes.addAndGet(frame.size.toLong())
        downRms = rmsOf(frame)
        if (!playQueue.offerLast(frame)) {
            playQueue.pollFirst()
            droppedFrames++
            playQueue.offerLast(frame)
        }
    }

    private fun startWriter() {
        val thread = Thread({
            while (running.get()) {
                val frame = try {
                    playQueue.poll(500, java.util.concurrent.TimeUnit.MILLISECONDS)
                } catch (_: InterruptedException) {
                    break
                } ?: continue
                val t = track ?: continue
                val written = t.write(frame, 0, frame.size, AudioTrack.WRITE_BLOCKING)
                if (written < 0) Log.w(TAG, "playback: write error $written")
            }
        }, "writer").also { it.start() }
        threads += thread
    }

    @SuppressLint("MissingPermission")
    private fun startCapture() {
        val minBuf = AudioRecord.getMinBufferSize(
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        if (minBuf <= 0) {
            Log.w(TAG, "capture: no input device (getMinBufferSize=$minBuf)")
            captureState = "unavailable"
            return
        }
        val rec = try {
            AudioRecord(
                MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minBuf * 4, FRAME_BYTES * 16),
            )
        } catch (t: Throwable) {
            Log.w(TAG, "capture: AudioRecord construction failed: ${t.javaClass.simpleName}: ${t.message}")
            null
        }
        if (rec == null || rec.state != AudioRecord.STATE_INITIALIZED) {
            Log.w(TAG, "capture: AudioRecord not initialised")
            rec?.release()
            captureState = "unavailable"
            return
        }
        enableCaptureEffects(rec)
        rec.startRecording()
        record = rec
        captureState = "recording"
        val thread = Thread({ captureLoop(rec) }, "capture").also { it.start() }
        threads += thread
    }

    private fun enableCaptureEffects(rec: AudioRecord) {
        try {
            if (AcousticEchoCanceler.isAvailable()) {
                aec = AcousticEchoCanceler.create(rec.audioSessionId)?.apply { enabled = true }
            }
            Log.i(TAG, "capture: AEC available=${AcousticEchoCanceler.isAvailable()} enabled=${aec?.enabled}")
            if (NoiseSuppressor.isAvailable()) {
                ns = NoiseSuppressor.create(rec.audioSessionId)?.apply { enabled = true }
            }
            Log.i(TAG, "capture: NS available=${NoiseSuppressor.isAvailable()} enabled=${ns?.enabled}")
        } catch (t: Throwable) {
            Log.w(TAG, "capture: effect init failed: ${t.message}")
        }
    }

    private fun captureLoop(rec: AudioRecord) {
        val samples = ShortArray(FRAME_SAMPLES)
        val bytes = ByteArray(FRAME_BYTES)
        while (running.get()) {
            val read = rec.read(samples, 0, FRAME_SAMPLES)
            if (read <= 0) {
                Log.w(TAG, "capture: read returned $read")
                continue
            }
            upRms = rmsOf(samples, read)
            for (i in 0 until read) {
                val v = samples[i].toInt()
                bytes[i * 2] = (v and 0xFF).toByte()
                bytes[i * 2 + 1] = ((v shr 8) and 0xFF).toByte()
            }
            transport?.sendPcm(if (read == FRAME_SAMPLES) bytes else bytes.copyOf(read * 2))
            upFrames.incrementAndGet()
            upBytes.addAndGet(read.toLong() * 2)
        }
    }

    private fun startGeneratedUpstream(freqHz: Double) {
        val thread = Thread({
            val bytes = ByteArray(FRAME_BYTES)
            val step = 2.0 * PI * freqHz / SAMPLE_RATE
            var nextAt = System.nanoTime()
            Log.i(TAG, "capture: generating $freqHz Hz upstream tone (no microphone capture)")
            while (running.get()) {
                for (i in 0 until FRAME_SAMPLES) {
                    val v = (sin(tonePhase) * 0.2 * 32767.0).toInt()
                    bytes[i * 2] = (v and 0xFF).toByte()
                    bytes[i * 2 + 1] = ((v shr 8) and 0xFF).toByte()
                    tonePhase += step
                    if (tonePhase > 2.0 * PI) tonePhase -= 2.0 * PI
                }
                upRms = 0.2 * 32767.0 / sqrt(2.0)
                transport?.sendPcm(bytes)
                upFrames.incrementAndGet()
                upBytes.addAndGet(FRAME_BYTES.toLong())
                nextAt += 20_000_000L
                val sleepNs = nextAt - System.nanoTime()
                if (sleepNs > 0) {
                    try {
                        Thread.sleep(sleepNs / 1_000_000L)
                    } catch (_: InterruptedException) {
                        break
                    }
                } else {
                    nextAt = System.nanoTime()
                }
            }
        }, "upstream-tone").also { it.start() }
        threads += thread
    }

    private fun startPlayback() {
        val minBuf = AudioTrack.getMinBufferSize(
            SAMPLE_RATE,
            AudioFormat.CHANNEL_OUT_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        if (minBuf <= 0) {
            Log.w(TAG, "playback: no output device (getMinBufferSize=$minBuf)")
            playbackState = "unavailable"
            return
        }
        val t = try {
            AudioTrack.Builder()
                .setAudioAttributes(
                    if (mediaUsage) {
                        AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                            .build()
                    } else {
                        AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                            .build()
                    },
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setSampleRate(SAMPLE_RATE)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .build(),
                )
                .setBufferSizeInBytes(maxOf(minBuf * 2, FRAME_BYTES * 16))
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build()
        } catch (t: Throwable) {
            Log.w(TAG, "playback: AudioTrack construction failed: ${t.javaClass.simpleName}: ${t.message}")
            null
        }
        if (t == null || t.state != AudioTrack.STATE_INITIALIZED) {
            Log.w(TAG, "playback: AudioTrack not initialised")
            t?.release()
            playbackState = "unavailable"
            return
        }
        t.play()
        track = t
        playbackState = "playing"
        Log.i(TAG, "playback: started rate=$SAMPLE_RATE channel=mono usage=${if (mediaUsage) "MEDIA" else "VOICE_COMMUNICATION"} play_state=${t.playState}")
    }

    private fun configureMode() {
        try {
            audioManager?.mode = if (mediaUsage) AudioManager.MODE_NORMAL else AudioManager.MODE_IN_COMMUNICATION
            Log.i(TAG, "audio: mode=${if (mediaUsage) "MODE_NORMAL" else "MODE_IN_COMMUNICATION"} media_usage=$mediaUsage")
        } catch (t: Throwable) {
            Log.w(TAG, "audio: setting mode failed: ${t.message}")
        }
    }

    private fun selectCommunicationDevice() {
        val am = audioManager ?: return
        val devices = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            am.availableCommunicationDevices
        } else {
            am.getDevices(AudioManager.GET_DEVICES_OUTPUTS).toList()
        }
        val summary = devices.joinToString { "type=${it.type}/${it.productName}" }
        Log.i(TAG, "route: candidates=[$summary]")
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) {
            routeInfo = "pre-S: framework routing only"
            return
        }
        val headset = devices.firstOrNull {
            it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO ||
                it.type == AudioDeviceInfo.TYPE_BLE_HEADSET ||
                it.type == AudioDeviceInfo.TYPE_WIRED_HEADSET ||
                it.type == AudioDeviceInfo.TYPE_USB_HEADSET
        }
        if (headset == null) {
            routeInfo = "no headset; framework default"
            Log.i(TAG, "route: no headset present, leaving framework default")
            return
        }
        val ok = try {
            am.setCommunicationDevice(headset)
        } catch (t: Throwable) {
            Log.w(TAG, "route: setCommunicationDevice failed: ${t.message}")
            false
        }
        routeInfo = "setCommunicationDevice(type=${headset.type})=$ok"
        Log.i(TAG, "route: requested type=${headset.type}/${headset.productName} -> $ok, active=${am.communicationDevice?.type}")
    }

    private fun registerDeviceCallback() {
        val am = audioManager ?: return
        deviceCallback = object : AudioDeviceCallback() {
            override fun onAudioDevicesAdded(addedDevices: Array<out AudioDeviceInfo>) {
                Log.i(TAG, "route: devices added [${addedDevices.joinToString { "type=${it.type}" }}]")
                recoverPlayback("devices-added")
            }

            override fun onAudioDevicesRemoved(removedDevices: Array<out AudioDeviceInfo>) {
                Log.i(TAG, "route: devices removed [${removedDevices.joinToString { "type=${it.type}" }}]")
                recoverPlayback("devices-removed")
            }
        }
        try {
            am.registerAudioDeviceCallback(deviceCallback, null)
            Log.i(TAG, "route: audio device callback registered")
        } catch (t: Throwable) {
            Log.w(TAG, "route: registering device callback failed: ${t.message}")
        }
    }

    private fun unregisterDeviceCallback() {
        val cb = deviceCallback ?: return
        try {
            audioManager?.unregisterAudioDeviceCallback(cb)
        } catch (_: Throwable) {
        }
        deviceCallback = null
    }

    private fun recoverPlayback(reason: String) {
        val t = track ?: return
        if (t.playState == AudioTrack.PLAYSTATE_PLAYING) return
        try {
            t.play()
            Log.i(TAG, "playback: restarted after $reason (state=${t.playState})")
        } catch (t2: Throwable) {
            Log.w(TAG, "playback: restart after $reason failed: ${t2.message}")
        }
    }

    private fun acquireWakeLock() {
        val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "HermesTalkSpike:call").apply {
            setReferenceCounted(false)
            acquire(60 * 60 * 1000L)
        }
        Log.i(TAG, "power: partial wake lock acquired for up to 60 min")
    }

    private fun startStats() {
        val thread = Thread({
            while (running.get()) {
                try {
                    Thread.sleep(5000)
                } catch (_: InterruptedException) {
                    break
                }
                val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
                val idle = pm.isDeviceIdleMode
                val exempt = pm.isIgnoringBatteryOptimizations(context.packageName)
                if (track?.playState != AudioTrack.PLAYSTATE_PLAYING) recoverPlayback("watchdog")
                val playhead = track?.playbackHeadPosition ?: -1
                Log.i(
                    TAG,
                    "stats: up_frames=${upFrames.get()} up_bytes=${upBytes.get()} " +
                        "up_rms=${"%.0f".format(upRms)} down_frames=${downFrames.get()} " +
                        "down_bytes=${downBytes.get()} down_rms=${"%.0f".format(downRms)} " +
                        "dropped=${droppedFrames} queued=${playQueue.size} " +
                        "capture=$captureState playback=$playbackState playhead=$playhead " +
                        "play_state=${track?.playState ?: -1} underruns=${track?.underrunCount ?: -1} " +
                        "device_idle=$idle battery_exempt=$exempt route=$routeInfo",
                )
            }
        }, "stats").also { it.start() }
        threads += thread
    }

    private companion object {
        const val PLAY_QUEUE_FRAMES = 8
    }

    private fun rmsOf(pcm: ByteArray): Double {
        var sum = 0.0
        var count = 0
        var i = 0
        while (i + 1 < pcm.size) {
            val v = ((pcm[i + 1].toInt() shl 8) or (pcm[i].toInt() and 0xFF)).toShort().toInt()
            sum += v.toDouble() * v.toDouble()
            count++
            i += 2
        }
        return if (count == 0) 0.0 else sqrt(sum / count)
    }

    private fun rmsOf(samples: ShortArray, count: Int): Double {
        var sum = 0.0
        for (i in 0 until count) {
            val v = samples[i].toDouble()
            sum += v * v
        }
        return if (count == 0) 0.0 else sqrt(sum / count)
    }
}
