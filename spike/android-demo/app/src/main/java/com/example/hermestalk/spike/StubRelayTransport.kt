package com.example.hermestalk.spike

import android.util.Log
import java.io.DataInputStream
import java.io.DataOutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

/**
 * Stub transport: uint32 big-endian length + PCM S16LE mono 16 kHz frames over a raw TCP socket.
 *
 * No TLS, no auth, no reconnection semantics beyond "retry every 2 s" — it exists only to prove the
 * audio path and the service lifecycle. See spike/stub-relay/relay.py for the other end.
 */
class StubRelayTransport(
    private val host: String,
    private val port: Int,
    private val onDownstreamFrame: (ByteArray) -> Unit,
) : Transport {

    private val running = AtomicBoolean(false)
    private val attempts = AtomicLong()
    private val framesSent = AtomicLong()
    private val framesReceived = AtomicLong()

    @Volatile private var socket: Socket? = null
    @Volatile private var output: DataOutputStream? = null
    private var thread: Thread? = null

    override fun connect() {
        if (running.getAndSet(true)) return
        thread = Thread({ ioLoop() }, "relay-io").also { it.start() }
    }

    private fun ioLoop() {
        while (running.get()) {
            val attempt = attempts.incrementAndGet()
            try {
                val s = Socket()
                s.tcpNoDelay = true
                s.connect(InetSocketAddress(host, port), CONNECT_TIMEOUT_MS)
                socket = s
                output = DataOutputStream(s.getOutputStream())
                Log.i(TAG, "relay: connected to $host:$port (attempt $attempt)")
                readLoop(s)
            } catch (t: Throwable) {
                Log.w(TAG, "relay: attempt $attempt failed: ${t.javaClass.simpleName}: ${t.message}")
            } finally {
                dropSocket()
            }
            if (!running.get()) break
            try {
                Thread.sleep(RETRY_MS)
            } catch (_: InterruptedException) {
                break
            }
        }
        Log.i(TAG, "relay: io loop stopped after ${attempts.get()} attempt(s)")
    }

    private fun readLoop(s: Socket) {
        val input = DataInputStream(s.getInputStream())
        while (running.get()) {
            val length = input.readInt()
            if (length <= 0 || length > MAX_FRAME) {
                Log.w(TAG, "relay: bad frame length $length, closing")
                return
            }
            val frame = ByteArray(length)
            input.readFully(frame)
            framesReceived.incrementAndGet()
            onDownstreamFrame(frame)
        }
    }

    override fun sendPcm(frame: ByteArray) {
        val out = output ?: return
        try {
            synchronized(this) {
                out.writeInt(frame.size)
                out.write(frame)
                out.flush()
            }
            framesSent.incrementAndGet()
        } catch (t: Throwable) {
            Log.w(TAG, "relay: send failed: ${t.message}")
            dropSocket()
        }
    }

    override fun close() {
        running.set(false)
        dropSocket()
        thread?.join(1000)
        thread = null
    }

    private fun dropSocket() {
        val s = socket
        socket = null
        output = null
        try {
            s?.close()
        } catch (_: Throwable) {
        }
    }

    private companion object {
        const val CONNECT_TIMEOUT_MS = 5000
        const val RETRY_MS = 2000L
        const val MAX_FRAME = 1 shl 20
    }
}
