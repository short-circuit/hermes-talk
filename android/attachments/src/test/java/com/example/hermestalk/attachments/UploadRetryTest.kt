package com.example.hermestalk.attachments

import com.example.hermestalk.rtvi.RelaySettings
import com.sun.net.httpserver.HttpServer
import java.io.File
import java.net.InetSocketAddress
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class UploadRetryTest {

    @get:Rule
    val folder = TemporaryFolder()

    @Test
    fun retries503ThenSucceeds() = runBlocking {
        val attempts = AtomicInteger(0)
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/v1/attachments") { exchange ->
            exchange.requestBody.readBytes()
            val count = attempts.incrementAndGet()
            if (count == 1) {
                val body = """
                    {"error":"upload_busy","detail":"busy","spoken":"The relay is busy saving another file. Try again in a moment.","retry_after":2}
                """.trimIndent().toByteArray()
                exchange.responseHeaders.add("Content-Type", "application/json")
                exchange.responseHeaders.add("Retry-After", "2")
                exchange.sendResponseHeaders(503, body.size.toLong())
                exchange.responseBody.use { it.write(body) }
            } else {
                val body = """{"attachment":{"id":"a1","sha256":"abc"},"spoken":"Picture saved."}"""
                    .toByteArray()
                exchange.responseHeaders.add("Content-Type", "application/json")
                exchange.sendResponseHeaders(201, body.size.toLong())
                exchange.responseBody.use { it.write(body) }
            }
        }
        server.start()
        try {
            val delays = mutableListOf<Long>()
            val runner = UploadRunner(
                engine = HttpUploadEngine(boundaryFactory = { "b" }),
                delayMillis = { millis -> delays += millis },
            )
            val result = runner.run(
                settings = settings(server.address.port),
                payload = payload(),
                onProgress = { _, _ -> },
            )
            assertTrue(result.outcome is UploadOutcome.Success)
            assertEquals(2, result.attempts)
            assertEquals(2, attempts.get())
            assertEquals(listOf(2000L), delays)
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun doesNotRetryTerminal415() = runBlocking {
        val attempts = AtomicInteger(0)
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/v1/attachments") { exchange ->
            exchange.requestBody.readBytes()
            attempts.incrementAndGet()
            val body = """
                {"error":"type_mismatch","spoken":"The file's contents do not match its name."}
            """.trimIndent().toByteArray()
            exchange.responseHeaders.add("Content-Type", "application/json")
            exchange.sendResponseHeaders(415, body.size.toLong())
            exchange.responseBody.use { it.write(body) }
        }
        server.start()
        try {
            val delays = mutableListOf<Long>()
            val result = UploadRunner(
                engine = HttpUploadEngine(boundaryFactory = { "b" }),
                delayMillis = { millis -> delays += millis },
            ).run(
                settings = settings(server.address.port),
                payload = payload(),
                onProgress = { _, _ -> },
            )
            assertTrue(result.outcome is UploadOutcome.Failure)
            assertEquals(1, result.attempts)
            assertEquals(1, attempts.get())
            assertTrue(delays.isEmpty())
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun retriesWhenTheRelayClosesTheConnectionMidBody() = runBlocking {
        val attempts = AtomicInteger(0)
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/v1/attachments") { exchange ->
            val count = attempts.incrementAndGet()
            if (count == 1) {
                exchange.requestBody.read(ByteArray(1024))
                exchange.close()
            } else {
                exchange.requestBody.readBytes()
                val body = """{"attachment":{"id":"a1","sha256":"abc"},"spoken":"File saved."}"""
                    .toByteArray()
                exchange.responseHeaders.add("Content-Type", "application/json")
                exchange.sendResponseHeaders(201, body.size.toLong())
                exchange.responseBody.use { it.write(body) }
            }
        }
        server.start()
        try {
            val delays = mutableListOf<Long>()
            val result = UploadRunner(
                engine = HttpUploadEngine(boundaryFactory = { "b" }),
                delayMillis = { millis -> delays += millis },
            ).run(
                settings = settings(server.address.port),
                payload = payload(sizeBytes = 1024 * 1024),
                onProgress = { _, _ -> },
            )
            assertTrue(result.outcome is UploadOutcome.Success)
            assertEquals(2, result.attempts)
            assertEquals(2, attempts.get())
            assertEquals(listOf(1000L), delays)
        } finally {
            server.stop(0)
        }
    }

    private fun settings(port: Int) = RelaySettings(
        relayUrl = "http://127.0.0.1:$port",
        deviceToken = "token",
    )

    private fun payload(sizeBytes: Int = 16): UploadPayload {
        val file = File(folder.newFolder(), "payload.bin")
        file.writeBytes(ByteArray(sizeBytes) { 0x42 })
        return UploadPayload(
            file = file,
            displayName = "test.png",
            mimeType = "image/png",
            sizeBytes = sizeBytes.toLong(),
            sha256 = "abc",
        )
    }
}
