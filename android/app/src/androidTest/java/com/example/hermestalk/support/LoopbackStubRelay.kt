package com.example.hermestalk.support

import java.io.BufferedInputStream
import java.io.IOException
import java.io.InputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.security.MessageDigest
import java.util.concurrent.CopyOnWriteArrayList

/** One request captured by [LoopbackStubRelay]. */
class StubRequest(
    val method: String,
    val path: String,
    val headers: Map<String, String>,
    val body: ByteArray,
) {
    val authorization: String? get() = headers["authorization"]

    /** The bearer credential, or null when the header is absent or malformed. */
    val bearerToken: String?
        get() = authorization?.let { value ->
            if (value.startsWith("Bearer ", ignoreCase = true)) {
                value.substring(BEARER_PREFIX_LENGTH).trim()
            } else {
                null
            }
        }

    val contentType: String? get() = headers["content-type"]

    val bodySha256: String get() = LoopbackStubRelay.sha256Hex(body)

    fun bodyContains(marker: String): Boolean =
        String(body, Charsets.ISO_8859_1).contains(marker)

    private companion object {
        const val BEARER_PREFIX_LENGTH = 7
    }
}

/** A scripted HTTP response. */
data class StubResponse(
    val status: Int,
    val body: String = "",
    val headers: Map<String, String> = emptyMap(),
    val contentType: String = "application/json",
)

/**
 * Minimal HTTP/1.1 fixture that runs inside the instrumented app process on the
 * loopback interface.
 *
 * It exists so upload retry, token expiry and artifact handling can be exercised
 * against real sockets without a host-side relay or an `adb reverse` forward,
 * which keeps those instrumentation tests runnable on a plain emulator in CI.
 * The socket binds 127.0.0.1 only, so no LAN address is ever hard-coded or
 * exposed.
 *
 * Scripting:
 * - [bearerToken] empty disables auth; otherwise every path requires exactly
 *   that bearer value and answers 401 when it does not match.
 * - [uploadStatuses] queues the statuses returned by `POST /v1/attachments`;
 *   once the queue is empty the endpoint answers 201.
 * - [rejectUploadsWith] forces every upload to fail with that status until it
 *   is set back to null.
 */
class LoopbackStubRelay(
    private val bearerToken: String = "",
    uploadStatuses: List<Int> = emptyList(),
    var healthStatus: Int = 200,
    var artifactsStatus: Int = 200,
) {

    private val socket = ServerSocket(0, BACKLOG, InetAddress.getByName(LOOPBACK))
    private val requests = CopyOnWriteArrayList<StubRequest>()
    private val uploadPlan = ArrayList(uploadStatuses)
    private val worker: Thread

    @Volatile
    private var running = true

    /** Forced status for every upload; null uses [uploadPlan] and then 201. */
    @Volatile
    var rejectUploadsWith: Int? = null

    /** The ephemeral loopback port the fixture listens on. */
    val port: Int get() = socket.localPort

    /** Base URL for `RelaySettings`, for example `http://127.0.0.1:41234`. */
    val baseUrl: String get() = "http://$LOOPBACK:$port"

    init {
        worker = Thread({ acceptLoop() }, "stub-relay-http").apply {
            isDaemon = true
            start()
        }
    }

    /** Every captured request, in arrival order. */
    fun requests(): List<StubRequest> = requests.toList()

    /** Captured requests for one method and path. */
    fun requestsFor(method: String, path: String): List<StubRequest> =
        requests.filter { it.method == method.uppercase() && it.path == path }

    /** Queue statuses for the next uploads. */
    fun enqueueUploadStatuses(vararg statuses: Int) {
        synchronized(uploadPlan) { uploadPlan.addAll(statuses.toList()) }
    }

    /** Stop accepting requests and close the listening socket. */
    fun close() {
        running = false
        runCatching { socket.close() }
        worker.interrupt()
    }

    private fun acceptLoop() {
        while (running) {
            val client = try {
                socket.accept()
            } catch (_: IOException) {
                break
            }
            try {
                handle(client)
            } catch (_: IOException) {
                // The client closed early; a fixture does not need to retry.
            } finally {
                runCatching { client.close() }
            }
        }
    }

    private fun handle(client: Socket) {
        val input = BufferedInputStream(client.getInputStream())
        val requestLine = readLine(input) ?: return
        val parts = requestLine.split(' ')
        if (parts.size < 2) {
            return
        }
        val method = parts[0].uppercase()
        val path = parts[1].substringBefore('?')
        val headers = readHeaders(input)
        val declaredLength = headers["content-length"]?.toLongOrNull() ?: 0L
        val body = readBody(input, declaredLength)
        val request = StubRequest(method, path, headers, body)
        requests += request
        writeResponse(client, respond(request))
    }

    private fun respond(request: StubRequest): StubResponse {
        if (bearerToken.isNotEmpty() && request.bearerToken != bearerToken) {
            return errorResponse(401)
        }
        return when {
            request.method == "GET" && request.path == "/health" ->
                if (healthStatus in 200..299) {
                    StubResponse(healthStatus, """{"status":"ok"}""")
                } else {
                    errorResponse(healthStatus)
                }

            request.method == "GET" && request.path == "/ready" ->
                StubResponse(200, """{"status":"ready"}""")

            request.method == "POST" && request.path == "/v1/attachments" -> upload(request)

            request.method == "GET" && request.path == "/v1/artifacts" ->
                if (artifactsStatus in 200..299) {
                    StubResponse(artifactsStatus, artifactListBody())
                } else {
                    errorResponse(artifactsStatus)
                }

            request.method == "GET" && request.path.startsWith("/v1/artifacts/") ->
                artifactResponse()

            else -> errorResponse(404)
        }
    }

    private fun upload(request: StubRequest): StubResponse {
        val status = rejectUploadsWith
            ?: synchronized(uploadPlan) { uploadPlan.removeFirstOrNull() }
            ?: 201
        if (status == 201) {
            val sha = request.bodySha256
            val size = request.body.size
            return StubResponse(
                201,
                """{"attachment":{"id":"stub-attachment","mime":"text/plain",""" +
                    """"kind":"document","sha256":"$sha","size_bytes":$size},""" +
                    """"spoken":"File saved."}""",
            )
        }
        return errorResponse(status)
    }

    private fun artifactResponse(): StubResponse = StubResponse(
        200,
        String(ARTIFACT_BYTES, Charsets.UTF_8),
        mapOf("X-Artifact-SHA256" to sha256Hex(ARTIFACT_BYTES)),
        contentType = "text/plain",
    )

    private fun artifactListBody(): String {
        val sha = sha256Hex(ARTIFACT_BYTES)
        return """{"artifacts":[{"id":"stub-artifact-1","name":"note.txt","mime":"text/plain",""" +
            """"size_bytes":${ARTIFACT_BYTES.size},"sha256":"$sha","created_at":"2026-01-01T00:00:00Z"}],""" +
            """"count":1,"limit":50,"retention_days":7}"""
    }

    private fun errorResponse(status: Int): StubResponse {
        val (code, spoken) = when (status) {
            401, 403 -> "unauthorized" to "The relay did not accept the device token."
            404 -> "not_found" to "The relay has no such resource."
            413 -> "file_too_large" to "That file is too large to send. Try a smaller one."
            415 -> "unsupported_type" to "That file type is not supported."
            503 -> "relay_unavailable" to
                "The relay cannot save the file right now. Try again in a moment."
            else -> "relay_error" to "The relay had a problem saving the file. Try again."
        }
        val headers = if (status == 503) mapOf("Retry-After" to "0") else emptyMap()
        return StubResponse(
            status,
            """{"error":"$code","detail":"scripted failure","spoken":"$spoken"}""",
            headers,
        )
    }

    private fun writeResponse(client: Socket, response: StubResponse) {
        val bytes = response.body.toByteArray(Charsets.UTF_8)
        val head = buildString {
            append("HTTP/1.1 ${response.status} ${reason(response.status)}\r\n")
            append("Content-Type: ${response.contentType}\r\n")
            append("Content-Length: ${bytes.size}\r\n")
            append("Connection: close\r\n")
            for ((name, value) in response.headers) {
                append("$name: $value\r\n")
            }
            append("\r\n")
        }
        val output = client.getOutputStream()
        output.write(head.toByteArray(Charsets.US_ASCII))
        output.write(bytes)
        output.flush()
    }

    private fun readHeaders(input: InputStream): Map<String, String> {
        val headers = LinkedHashMap<String, String>()
        while (true) {
            val line = readLine(input) ?: break
            if (line.isEmpty()) {
                break
            }
            val separator = line.indexOf(':')
            if (separator <= 0) {
                continue
            }
            headers[line.substring(0, separator).trim().lowercase()] =
                line.substring(separator + 1).trim()
        }
        return headers
    }

    private fun readBody(input: InputStream, declaredLength: Long): ByteArray {
        val length = declaredLength.coerceIn(0L, MAX_BODY_BYTES)
        val out = java.io.ByteArrayOutputStream()
        val buffer = ByteArray(CHUNK_BYTES)
        var remaining = length
        while (remaining > 0) {
            val read = input.read(buffer, 0, minOf(buffer.size.toLong(), remaining).toInt())
            if (read < 0) {
                break
            }
            out.write(buffer, 0, read)
            remaining -= read
        }
        return out.toByteArray()
    }

    private fun readLine(input: InputStream): String? {
        val line = StringBuilder()
        while (true) {
            val value = input.read()
            if (value < 0) {
                return if (line.isEmpty()) null else line.toString()
            }
            val character = value.toChar()
            if (character == '\n') {
                return line.toString().removeSuffix("\r")
            }
            line.append(character)
            if (line.length > MAX_LINE_CHARS) {
                return line.toString()
            }
        }
    }

    private fun reason(status: Int): String = when (status) {
        200 -> "OK"
        201 -> "Created"
        400 -> "Bad Request"
        401 -> "Unauthorized"
        403 -> "Forbidden"
        404 -> "Not Found"
        413 -> "Payload Too Large"
        415 -> "Unsupported Media Type"
        503 -> "Service Unavailable"
        else -> "Error"
    }

    companion object {

        /** Payload the fixture serves for artifact downloads. */
        val ARTIFACT_BYTES: ByteArray = "stub artifact payload".toByteArray(Charsets.UTF_8)

        /** SHA-256 of [bytes] as lowercase hex. */
        fun sha256Hex(bytes: ByteArray): String =
            MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { byte ->
                "%02x".format(byte)
            }

        private const val LOOPBACK = "127.0.0.1"
        private const val BACKLOG = 64
        private const val CHUNK_BYTES = 16 * 1024
        private const val MAX_BODY_BYTES = 8L * 1024L * 1024L
        private const val MAX_LINE_CHARS = 8 * 1024
    }
}
