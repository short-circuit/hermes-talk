package com.example.hermestalk.spike

/**
 * The seam where a real client transport goes.
 *
 * The spike ships [StubRelayTransport] (length-prefixed PCM over TCP). Replacing it with
 * Pipecat's `PipecatClientSmallWebRTC` or a LiveKit `Room` must not change [AudioEngine]:
 * both expose the same two operations, "push captured audio out" and "hand rendered audio in".
 */
interface Transport {
    fun connect()
    fun sendPcm(frame: ByteArray)
    fun close()
}
