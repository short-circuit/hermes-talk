package com.example.hermestalk.attachments

import com.example.hermestalk.rtvi.RelaySettings
import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
import java.io.File
import java.net.InetSocketAddress
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class UploadEngineTest {

    @get:Rule
    val folder = TemporaryFolder()

    @Test
    fun sendsFixedLengthMultipartWithBearerTokenAndParses201() = runBlocking {
        val payloadBytes = byteArrayOf(0x89.toByte(), 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3)
        val sha = java.security.MessageDigest.getInstance("SHA-256")
            .digest(payloadBytes)
            .toHex()
        val captured = AtomicReference<CapturedRequest>()
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/v1/attachments") { exchange ->
            captured.set(CapturedRequest.from(exchange))
            val body = """
                {"attachment":{"id":"a1","mime":"image/png","kind":"image","sha256":"$sha",
                "content_sha256":"$sha","size_bytes":${payloadBytes.size},"uploaded_bytes":${payloadBytes.size},
                "detected_mime":"image/png","facts":{},"original_name":"test.png",
                "created_at":"2026-01-01T00:00:00Z","path":"inbox/test.png"},"spoken":"Picture saved."}
            """.trimIndent().toByteArray()
            exchange.responseHeaders.add("Content-Type", "application/json")
            exchange.sendResponseHeaders(201, body.size.toLong())
            exchange.responseBody.use { it.write(body) }
        }
        server.start()
        try {
            val payloadFile = File(folder.newFolder("payload"), "payload.bin")
            payloadFile.writeBytes(payloadBytes)
            val settings = RelaySettings(
                relayUrl = "http://127.0.0.1:${server.address.port}",
                deviceToken = "test-token",
            )
            val engine = HttpUploadEngine(boundaryFactory = { "test-boundary" })
            val progress = mutableListOf<Pair<Long, Long>>()
            val outcome = engine.upload(
                settings = settings,
                payload = UploadPayload(
                    file = payloadFile,
                    displayName = "test.png",
                    mimeType = "image/png",
                    sizeBytes = payloadBytes.size.toLong(),
                    sha256 = sha,
                ),
                onProgress = { uploaded, total -> progress += uploaded to total },
            )

            assertTrue(outcome is UploadOutcome.Success)
            val success = outcome as UploadOutcome.Success
            assertEquals("a1", success.result.attachmentId)
            assertEquals("Picture saved.", success.result.spoken)
            assertEquals(sha, success.result.sha256)

            val request = captured.get()
            assertEquals("POST", request.method)
            assertEquals("/v1/attachments", request.path)
            assertEquals("Bearer test-token", request.authorization)
            assertEquals(
                "multipart/form-data; boundary=test-boundary",
                request.contentType,
            )
            assertEquals(request.body.size.toString(), request.contentLength)

            val expected = buildString {
                append("--test-boundary\r\n")
                append("Content-Disposition: form-data; name=\"file\"; filename=\"test.png\"\r\n")
                append("Content-Type: image/png\r\n")
                append("\r\n")
            }.toByteArray() + payloadBytes + buildString {
                append("\r\n--test-boundary--\r\n")
            }.toByteArray()
            assertArrayEquals(expected, request.body)
            assertEquals(payloadBytes.size.toLong(), progress.last().first)
            assertEquals(payloadBytes.size.toLong(), progress.last().second)
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun mapsRelayRejectionToSpokenFailure() = runBlocking {
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/v1/attachments") { exchange ->
            exchange.requestBody.readBytes()
            val body = """
                {"error":"unsupported_type","detail":"bad magic","spoken":"That file type is not supported.","retry_after":null}
            """.trimIndent().toByteArray()
            exchange.responseHeaders.add("Content-Type", "application/json")
            exchange.sendResponseHeaders(415, body.size.toLong())
            exchange.responseBody.use { it.write(body) }
        }
        server.start()
        try {
            val payloadFile = File(folder.newFolder("payload"), "payload.bin")
            payloadFile.writeBytes(byteArrayOf(1, 2, 3))
            val outcome = HttpUploadEngine(boundaryFactory = { "b" }).upload(
                settings = RelaySettings(
                    relayUrl = "http://127.0.0.1:${server.address.port}",
                    deviceToken = "token",
                ),
                payload = UploadPayload(
                    file = payloadFile,
                    displayName = "file.png",
                    mimeType = "image/png",
                    sizeBytes = 3,
                    sha256 = "abc",
                ),
                onProgress = { _, _ -> },
            )
            assertTrue(outcome is UploadOutcome.Failure)
            val failure = (outcome as UploadOutcome.Failure).failure
            assertEquals("unsupported_type", failure.code)
            assertEquals("That file type is not supported.", failure.spoken)
            assertEquals(false, failure.retryable)
            assertEquals(415, failure.httpStatus)
        } finally {
            server.stop(0)
        }
    }

    private data class CapturedRequest(
        val method: String,
        val path: String,
        val authorization: String?,
        val contentType: String?,
        val contentLength: String?,
        val body: ByteArray,
    ) {
        companion object {
            fun from(exchange: HttpExchange): CapturedRequest = CapturedRequest(
                method = exchange.requestMethod,
                path = exchange.requestURI.path,
                authorization = exchange.requestHeaders.getFirst("Authorization"),
                contentType = exchange.requestHeaders.getFirst("Content-Type"),
                contentLength = exchange.requestHeaders.getFirst("Content-Length"),
                body = exchange.requestBody.readBytes(),
            )
        }
    }
}
