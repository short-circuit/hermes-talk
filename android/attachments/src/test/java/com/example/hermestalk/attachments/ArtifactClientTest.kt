package com.example.hermestalk.attachments

import com.example.hermestalk.rtvi.RelaySettings
import com.sun.net.httpserver.HttpServer
import java.io.File
import java.net.InetSocketAddress
import java.security.MessageDigest
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class ArtifactClientTest {

    @get:Rule
    val folder = TemporaryFolder()

    @Test
    fun listsArtifactsWithBearerAuth() = runBlocking {
        val seenAuthorization = arrayOfNulls<String>(1)
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/v1/artifacts") { exchange ->
            seenAuthorization[0] = exchange.requestHeaders.getFirst("Authorization")
            val body = """
                {"artifacts":[{"id":"a.txt","name":"a.txt","mime":"text/plain","size_bytes":3,
                "sha256":"abc","created_at":"2026-01-01T00:00:00Z","expires_at":null}],
                "count":1,"limit":50,"retention_days":0}
            """.trimIndent().toByteArray()
            exchange.responseHeaders.add("Content-Type", "application/json")
            exchange.sendResponseHeaders(200, body.size.toLong())
            exchange.responseBody.use { it.write(body) }
        }
        server.start()
        try {
            val client = ArtifactClient(folder.newFolder("cache"))
            val outcome = client.list(settings(server.address.port))
            assertTrue(outcome is ArtifactListOutcome.Success)
            assertEquals("a.txt", (outcome as ArtifactListOutcome.Success).list.artifacts.first().id)
            assertEquals("Bearer token", seenAuthorization[0])
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun downloadsAndVerifiesSha256() = runBlocking {
        val bytes = "hello artifact".toByteArray()
        val sha = MessageDigest.getInstance("SHA-256").digest(bytes).toHex()
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/v1/artifacts/a.txt") { exchange ->
            exchange.responseHeaders.add("Content-Type", "text/plain")
            exchange.responseHeaders.add("X-Artifact-SHA256", sha)
            exchange.sendResponseHeaders(200, bytes.size.toLong())
            exchange.responseBody.use { it.write(bytes) }
        }
        server.start()
        try {
            val cache = folder.newFolder("cache")
            val client = ArtifactClient(cache)
            val outcome = client.download(
                settings = settings(server.address.port),
                artifact = artifact("a.txt", "text/plain", sha, bytes.size.toLong()),
                onProgress = { _, _ -> },
            )
            assertTrue(outcome is ArtifactDownloadOutcome.Success)
            val file = (outcome as ArtifactDownloadOutcome.Success).file
            assertTrue(file.exists())
            assertTrue(file.readBytes().contentEquals(bytes))
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun rejectsAndDeletesADownloadWithTheWrongChecksum() = runBlocking {
        val bytes = "hello artifact".toByteArray()
        val wrongSha = MessageDigest.getInstance("SHA-256").digest("different".toByteArray()).toHex()
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/v1/artifacts/a.txt") { exchange ->
            exchange.responseHeaders.add("Content-Type", "text/plain")
            exchange.responseHeaders.add("X-Artifact-SHA256", wrongSha)
            exchange.sendResponseHeaders(200, bytes.size.toLong())
            exchange.responseBody.use { it.write(bytes) }
        }
        server.start()
        try {
            val cache = folder.newFolder("cache")
            val client = ArtifactClient(cache)
            val outcome = client.download(
                settings = settings(server.address.port),
                artifact = artifact("a.txt", "text/plain", wrongSha, bytes.size.toLong()),
                onProgress = { _, _ -> },
            )
            assertTrue(outcome is ArtifactDownloadOutcome.Failure)
            assertEquals("sha256_mismatch", (outcome as ArtifactDownloadOutcome.Failure).error.code)
            val files = File(cache, "artifacts").listFiles().orEmpty()
            assertTrue(files.none { it.name.endsWith(".part") })
            assertTrue(files.isEmpty())
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun surfacesTheSpokenReasonFor404() = runBlocking {
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/v1/artifacts/missing.txt") { exchange ->
            val body = """
                {"error":"artifact_not_found","spoken":"That artifact could not be found."}
            """.trimIndent().toByteArray()
            exchange.responseHeaders.add("Content-Type", "application/json")
            exchange.sendResponseHeaders(404, body.size.toLong())
            exchange.responseBody.use { it.write(body) }
        }
        server.start()
        try {
            val outcome = ArtifactClient(folder.newFolder("cache")).download(
                settings = settings(server.address.port),
                artifact = artifact("missing.txt", "text/plain", "abc", 1),
                onProgress = { _, _ -> },
            )
            assertTrue(outcome is ArtifactDownloadOutcome.Failure)
            val error = (outcome as ArtifactDownloadOutcome.Failure).error
            assertEquals("artifact_not_found", error.code)
            assertEquals("That artifact could not be found.", error.spoken)
            assertEquals(404, error.httpStatus)
        } finally {
            server.stop(0)
        }
    }

    private fun settings(port: Int) = RelaySettings(
        relayUrl = "http://127.0.0.1:$port",
        deviceToken = "token",
    )

    private fun artifact(id: String, mime: String, sha: String, size: Long) = ArtifactInfo(
        id = id,
        name = id,
        mime = mime,
        sizeBytes = size,
        sha256 = sha,
    )
}
