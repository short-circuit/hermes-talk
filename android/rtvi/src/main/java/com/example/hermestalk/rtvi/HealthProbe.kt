package com.example.hermestalk.rtvi

import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** Result of a relay liveness probe. */
sealed interface ProbeResult {
    /** The relay answered with HTTP 200. */
    data object Ok : ProbeResult

    /** The probe failed. [detail] is safe to show in the UI. */
    data class Failure(val reason: FailureReason, val detail: String) : ProbeResult
}

/**
 * Liveness probe abstraction. A successful probe only proves that `/health`
 * responds; it does not prove that the RTVI handshake will succeed. The real
 * handshake remains the authority for readiness.
 */
interface HealthProbe {
    /** Probe [endpoint] using [deviceToken] as a bearer credential. */
    suspend fun probe(endpoint: RelayEndpoint, deviceToken: String): ProbeResult
}

/**
 * HTTP implementation of [HealthProbe]. It never logs the device token and caps
 * response snippets at [MAX_SNIPPET] characters.
 */
class HttpHealthProbe(private val timeoutMs: Int = 3000) : HealthProbe {

    override suspend fun probe(endpoint: RelayEndpoint, deviceToken: String): ProbeResult =
        withContext(Dispatchers.IO) {
            var connection: HttpURLConnection? = null
            try {
                connection = URL(endpoint.healthUrl()).openConnection() as HttpURLConnection
                connection.requestMethod = "GET"
                connection.connectTimeout = timeoutMs
                connection.readTimeout = timeoutMs
                if (deviceToken.isNotBlank()) {
                    connection.setRequestProperty("Authorization", "Bearer $deviceToken")
                }

                val status = connection.responseCode
                val stream = if (status in 200..299) connection.inputStream else connection.errorStream
                val body = stream?.bufferedReader()?.use { reader ->
                    reader.readText().take(MAX_SNIPPET)
                }
                ProbeMapping.fromStatus(status, body)
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (error: IOException) {
                ProbeResult.Failure(
                    FailureReason.Unreachable,
                    (error.message ?: "Connection failed").take(MAX_SNIPPET),
                )
            } catch (error: Exception) {
                ProbeResult.Failure(
                    FailureReason.Unexpected,
                    (error.message ?: "Unexpected probe failure").take(MAX_SNIPPET),
                )
            } finally {
                connection?.disconnect()
            }
        }

    private companion object {
        const val MAX_SNIPPET = 200
    }
}

/**
 * Pure mapping from an HTTP status code and response snippet to a [ProbeResult].
 * A status of 0 represents a transport-level failure (connection refused,
 * timeout, DNS failure) and maps to [FailureReason.Unreachable].
 */
object ProbeMapping {

    private const val MAX_SNIPPET = 200

    /** Map [status] and an optional [bodySnippet] to a probe result. */
    fun fromStatus(status: Int, bodySnippet: String?): ProbeResult {
        val snippet = bodySnippet?.take(MAX_SNIPPET)?.trim().orEmpty()
        return when {
            status == 200 -> ProbeResult.Ok
            status in 200..299 -> ProbeResult.Ok
            status == 401 || status == 403 ->
                ProbeResult.Failure(FailureReason.Unauthorized, withStatus("Unauthorized", status, snippet))

            status == 503 ->
                ProbeResult.Failure(FailureReason.NotReady, withStatus("Relay not ready", status, snippet))

            status <= 0 ->
                ProbeResult.Failure(
                    FailureReason.Unreachable,
                    snippet.ifBlank { "Relay unreachable" },
                )

            else ->
                ProbeResult.Failure(FailureReason.Unexpected, withStatus("Unexpected response", status, snippet))
        }
    }

    private fun withStatus(prefix: String, status: Int, snippet: String): String {
        val base = "$prefix (HTTP $status)"
        return if (snippet.isBlank()) base else "$base: $snippet".take(MAX_SNIPPET)
    }
}
