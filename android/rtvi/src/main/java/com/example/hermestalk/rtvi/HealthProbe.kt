package com.example.hermestalk.rtvi

import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.security.cert.CertPathBuilderException
import java.security.cert.CertPathValidatorException
import java.security.cert.CertificateException
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLException
import javax.net.ssl.SSLPeerUnverifiedException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** Result of a relay liveness probe. */
sealed interface ProbeResult {
    /** The relay answered with HTTP 200. */
    data object Ok : ProbeResult

    /**
     * The probe failed. [detail] is safe to show in the UI.
     *
     * [expectedPin] and [observedPin] are populated only for a TLS pin
     * mismatch; both are null when the certificate chain was simply untrusted.
     */
    data class Failure(
        val reason: FailureReason,
        val detail: String,
        val expectedPin: String? = null,
        val observedPin: String? = null,
    ) : ProbeResult
}

/**
 * Liveness probe abstraction. A successful probe only proves that `/health`
 * responds; it does not prove that the RTVI handshake will succeed. The real
 * handshake remains the authority for readiness.
 */
interface HealthProbe {
    /**
     * Probe [endpoint] using [deviceToken] as a bearer credential and [policy]
     * as the TLS trust decision.
     */
    suspend fun probe(
        endpoint: RelayEndpoint,
        deviceToken: String,
        policy: TlsPolicy,
    ): ProbeResult
}

/**
 * HTTP implementation of [HealthProbe]. It never logs the device token and caps
 * response snippets at [MAX_SNIPPET] characters.
 *
 * TLS *trust* failures are always reported as [FailureReason.TlsRejected], never
 * as [FailureReason.Unreachable], so the connection machine does not silently
 * retry them. A transport-level TLS failure (the peer closed the connection or
 * the handshake timed out) stays [FailureReason.Unreachable]: the certificate
 * was never judged, so it is not a trust decision.
 */
class HttpHealthProbe(private val timeoutMs: Int = 3000) : HealthProbe {

    override suspend fun probe(
        endpoint: RelayEndpoint,
        deviceToken: String,
        policy: TlsPolicy,
    ): ProbeResult = withContext(Dispatchers.IO) {
        var connection: HttpURLConnection? = null
        try {
            val opened = URL(endpoint.healthUrl()).openConnection()
            connection = opened as HttpURLConnection
            if (policy is TlsPolicy.Pinned) {
                (opened as? HttpsURLConnection)?.sslSocketFactory =
                    TlsTrust.socketFactoryForPin(policy.pin)
            }
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
        } catch (error: SSLException) {
            if (isTrustFailure(error)) tlsFailure(error) else transportFailure(error)
        } catch (error: CertificateException) {
            tlsFailure(error)
        } catch (error: IOException) {
            if (isTrustFailure(error)) {
                tlsFailure(error)
            } else {
                transportFailure(error)
            }
        } catch (error: Exception) {
            ProbeResult.Failure(
                FailureReason.Unexpected,
                (error.message ?: "Unexpected probe failure").take(MAX_SNIPPET),
            )
        } finally {
            connection?.disconnect()
        }
    }

    private fun transportFailure(error: Throwable): ProbeResult.Failure =
        ProbeResult.Failure(
            FailureReason.Unreachable,
            (error.message ?: "Connection failed").take(MAX_SNIPPET),
        )

    private fun tlsFailure(error: Throwable): ProbeResult.Failure {
        val mismatch = findPinMismatch(error)
        if (mismatch != null) {
            return ProbeResult.Failure(
                FailureReason.TlsRejected,
                (mismatch.message ?: "Certificate pin mismatch").take(MAX_SNIPPET),
                mismatch.expectedPin,
                mismatch.observedPin,
            )
        }
        val reason = shortReason(error)
        return ProbeResult.Failure(
            FailureReason.TlsRejected,
            "The relay certificate was not trusted: $reason".take(MAX_SNIPPET),
        )
    }

    private fun findPinMismatch(error: Throwable): PinMismatchException? {
        var current: Throwable? = error
        val seen = mutableSetOf<Throwable>()
        while (current != null && seen.add(current)) {
            if (current is PinMismatchException) {
                return current
            }
            current = current.cause
        }
        return null
    }

    /**
     * True only when the failure is a *trust* decision: a pin mismatch, a chain
     * that did not validate (CertificateException on Android, a CertPath* error
     * on the JVM) or a hostname/peer verification failure. A bare SSLException
     * such as "connection closed" means the certificate was never judged.
     */
    private fun isTrustFailure(error: Throwable): Boolean {
        var current: Throwable? = error
        val seen = mutableSetOf<Throwable>()
        while (current != null && seen.add(current)) {
            when (current) {
                is PinMismatchException,
                is CertificateException,
                is CertPathValidatorException,
                is CertPathBuilderException,
                is SSLPeerUnverifiedException,
                -> return true
            }
            current = current.cause
        }
        return false
    }

    private fun shortReason(error: Throwable): String {
        var current: Throwable? = error
        var lastMessage: String? = null
        val seen = mutableSetOf<Throwable>()
        while (current != null && seen.add(current)) {
            val message = current.message?.takeIf { it.isNotBlank() }
            if (message != null) {
                lastMessage = message
            }
            current = current.cause
        }
        return (lastMessage ?: error.javaClass.simpleName).take(MAX_SNIPPET)
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
