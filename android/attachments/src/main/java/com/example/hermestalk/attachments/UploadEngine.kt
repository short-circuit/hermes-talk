package com.example.hermestalk.attachments

import com.example.hermestalk.rtvi.PinMismatchException
import com.example.hermestalk.rtvi.RelayEndpoint
import com.example.hermestalk.rtvi.RelaySettings
import com.example.hermestalk.rtvi.TlsPolicy
import com.example.hermestalk.rtvi.TlsTrust
import com.example.hermestalk.rtvi.tlsPolicy
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.SocketTimeoutException
import java.net.URL
import java.security.cert.CertificateException
import java.util.UUID
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLPeerUnverifiedException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.Serializable
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json

/** One persisted payload ready to upload. */
data class UploadPayload(
    val file: File,
    val displayName: String,
    val mimeType: String?,
    val sizeBytes: Long,
    val sha256: String,
)

/** Parsed 201 response. */
data class UploadSuccess(
    val attachmentId: String?,
    val spoken: String,
    val sha256: String?,
    val sizeBytes: Long?,
    val detectedMime: String?,
    val kind: String?,
)

/**
 * A relay rejection or transport failure. [retryable] is the engine's first
 * assessment; [RetryPolicy] applies the final bounded decision.
 */
data class UploadFailure(
    val code: String,
    val spoken: String,
    val retryable: Boolean,
    val retryAfterSeconds: Long? = null,
    val httpStatus: Int = 0,
)

sealed interface UploadOutcome {
    data class Success(val result: UploadSuccess) : UploadOutcome

    data class Failure(val failure: UploadFailure) : UploadOutcome
}

/** Transport seam so the retry runner can be tested without a socket. */
interface UploadTransport {
    suspend fun upload(
        settings: RelaySettings,
        payload: UploadPayload,
        onProgress: (uploadedBytes: Long, totalBytes: Long) -> Unit,
    ): UploadOutcome
}

/**
 * Authenticated multipart upload over [HttpURLConnection]. It honours the same
 * TLS policy as `HealthProbe`: a configured pin replaces CA trust and fails
 * closed; platform-CA mode uses the platform store; loopback cleartext is only
 * used when the endpoint is plain HTTP. The device token is never logged.
 */
class HttpUploadEngine(
    private val boundaryFactory: () -> String = {
        "----hermes-talk-" + UUID.randomUUID().toString().replace("-", "")
    },
    private val connectTimeoutMs: Int = 15_000,
    private val readTimeoutMs: Int = 60_000,
) : UploadTransport {

    override suspend fun upload(
        settings: RelaySettings,
        payload: UploadPayload,
        onProgress: (uploadedBytes: Long, totalBytes: Long) -> Unit,
    ): UploadOutcome = withContext(Dispatchers.IO) {
        if (settings.deviceToken.isBlank()) {
            return@withContext UploadOutcome.Failure(
                UploadFailure(
                    code = "missing_token",
                    spoken = "No device token is configured.",
                    retryable = false,
                ),
            )
        }
        val endpoint = (RelayEndpoint.parse(settings.relayUrl) as? RelayEndpoint.ParseResult.Valid)
            ?.endpoint
            ?: return@withContext UploadOutcome.Failure(
                UploadFailure(
                    code = "invalid_settings",
                    spoken = "The relay address is not valid.",
                    retryable = false,
                ),
            )
        if (!payload.file.exists() || payload.file.length() != payload.sizeBytes) {
            return@withContext UploadOutcome.Failure(
                UploadFailure(
                    code = "payload_changed",
                    spoken = "The saved file changed before it could be sent.",
                    retryable = false,
                ),
            )
        }

        val boundary = boundaryFactory()
        val multipart = MultipartBody(
            boundary = boundary,
            filename = payload.displayName,
            contentType = payload.mimeType,
            payloadSize = payload.sizeBytes,
        )

        var connection: HttpURLConnection? = null
        try {
            connection = URL(endpoint.baseUrl() + "/v1/attachments").openConnection() as HttpURLConnection
            applyTls(connection, settings.tlsPolicy())
            connection.requestMethod = "POST"
            connection.doOutput = true
            connection.useCaches = false
            connection.connectTimeout = connectTimeoutMs
            connection.readTimeout = readTimeoutMs
            connection.setRequestProperty("Authorization", "Bearer ${settings.deviceToken}")
            connection.setRequestProperty(
                "Content-Type",
                "multipart/form-data; boundary=$boundary",
            )
            connection.setFixedLengthStreamingMode(multipart.contentLength)
            connection.setRequestProperty("Content-Length", multipart.contentLength.toString())

            connection.outputStream.use { output ->
                output.write(multipart.prefix)
                var uploaded = 0L
                var lastReported = 0L
                payload.file.inputStream().use { input ->
                    val buffer = ByteArray(CHUNK_BYTES)
                    while (true) {
                        val read = input.read(buffer)
                        if (read < 0) {
                            break
                        }
                        if (read == 0) {
                            continue
                        }
                        output.write(buffer, 0, read)
                        uploaded += read
                        if (uploaded - lastReported >= PROGRESS_INTERVAL_BYTES) {
                            lastReported = uploaded
                            onProgress(uploaded, payload.sizeBytes)
                        }
                    }
                }
                output.write(multipart.suffix)
                output.flush()
                onProgress(payload.sizeBytes, payload.sizeBytes)
            }

            val status = connection.responseCode
            val body = readBody(connection, status)
            if (status == 201) {
                parseSuccess(body)
            } else {
                UploadOutcome.Failure(
                    UploadErrorMapping.fromStatus(
                        status = status,
                        body = body,
                        retryAfterHeader = connection.getHeaderField("Retry-After"),
                    ),
                )
            }
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (error: IOException) {
            UploadOutcome.Failure(transportFailure(error))
        } catch (error: Exception) {
            UploadOutcome.Failure(
                UploadFailure(
                    code = "client_error",
                    spoken = "The file could not be sent.",
                    retryable = false,
                ),
            )
        } finally {
            connection?.disconnect()
        }
    }

    private fun applyTls(connection: HttpURLConnection, policy: TlsPolicy) {
        if (policy is TlsPolicy.Pinned) {
            (connection as? HttpsURLConnection)?.sslSocketFactory =
                TlsTrust.socketFactoryForPin(policy.pin)
        }
    }

    private fun readBody(connection: HttpURLConnection, status: Int): String {
        val stream = if (status in 200..299) {
            connection.inputStream
        } else {
            connection.errorStream
        }
        return stream?.bufferedReader(Charsets.UTF_8)?.use { reader ->
            val buffer = CharArray(MAX_BODY_CHARS)
            val read = reader.read(buffer)
            if (read <= 0) "" else String(buffer, 0, read)
        }.orEmpty()
    }

    private fun parseSuccess(body: String): UploadOutcome {
        return try {
            val response = UploadJson.json.decodeFromString<UploadResponse>(body)
            val attachment = response.attachment
            val spoken = response.spoken?.trim()?.takeIf { it.isNotEmpty() }
                ?: "File saved."
            if (attachment?.id.isNullOrBlank()) {
                UploadOutcome.Failure(
                    UploadFailure(
                        code = "bad_response",
                        spoken = "The relay did not confirm the upload.",
                        retryable = false,
                        httpStatus = 201,
                    ),
                )
            } else {
                UploadOutcome.Success(
                    UploadSuccess(
                        attachmentId = attachment.id,
                        spoken = spoken,
                        sha256 = attachment.sha256,
                        sizeBytes = attachment.size_bytes,
                        detectedMime = attachment.detected_mime,
                        kind = attachment.kind,
                    ),
                )
            }
        } catch (_: Exception) {
            UploadOutcome.Failure(
                UploadFailure(
                    code = "bad_response",
                    spoken = "The relay did not confirm the upload.",
                    retryable = false,
                    httpStatus = 201,
                ),
            )
        }
    }

    private fun transportFailure(error: Throwable): UploadFailure {
        val trustFailure = findCause(error) { cause ->
            cause is PinMismatchException ||
                cause is CertificateException ||
                cause is SSLPeerUnverifiedException
        }
        if (trustFailure) {
            return UploadFailure(
                code = "tls_rejected",
                spoken = "The relay certificate was not trusted.",
                retryable = false,
            )
        }
        val message = when (error) {
            is SocketTimeoutException -> "The relay did not answer in time."
            else -> "The relay could not be reached."
        }
        return UploadFailure(
            code = "transport",
            spoken = message,
            retryable = true,
        )
    }

    private inline fun findCause(error: Throwable, predicate: (Throwable) -> Boolean): Boolean {
        var current: Throwable? = error
        val seen = mutableSetOf<Throwable>()
        while (current != null && seen.add(current)) {
            if (predicate(current)) {
                return true
            }
            current = current.cause
        }
        return false
    }

    private companion object {
        const val CHUNK_BYTES = 32 * 1024
        const val PROGRESS_INTERVAL_BYTES = 32L * 1024L
        const val MAX_BODY_CHARS = 64 * 1024
    }
}

@Serializable
internal data class UploadResponse(
    val attachment: UploadAttachmentDto? = null,
    val spoken: String? = null,
)

@Serializable
internal data class UploadAttachmentDto(
    val id: String? = null,
    val mime: String? = null,
    val kind: String? = null,
    val sha256: String? = null,
    val content_sha256: String? = null,
    val size_bytes: Long? = null,
    val uploaded_bytes: Long? = null,
    val detected_mime: String? = null,
    val original_name: String? = null,
    val created_at: String? = null,
    val path: String? = null,
)

@Serializable
internal data class ErrorBody(
    val error: String? = null,
    val detail: String? = null,
    val spoken: String? = null,
    val retry_after: Long? = null,
)

internal object UploadJson {
    val json = Json {
        ignoreUnknownKeys = true
        encodeDefaults = true
    }
}

/**
 * Pure mapping from an HTTP status and relay error body to [UploadFailure]. It
 * is separate from the socket code so the status taxonomy is testable.
 */
object UploadErrorMapping {

    private val terminalStatuses = setOf(400, 401, 403, 409, 413, 415, 422)

    fun fromStatus(status: Int, body: String?, retryAfterHeader: String?): UploadFailure {
        val parsed = runCatching {
            UploadJson.json.decodeFromString<ErrorBody>(body.orEmpty())
        }.getOrNull()
        val code = parsed?.error?.trim()?.takeIf { it.isNotEmpty() }
            ?: defaultCode(status)
        val spoken = parsed?.spoken?.trim()?.takeIf { it.isNotEmpty() }
            ?: defaultSpoken(code, status)
        val retryAfter = parseRetryAfter(retryAfterHeader) ?: parsed?.retry_after
        val retryable = when {
            status in terminalStatuses -> false
            status == 503 -> true
            status in 500..599 -> true
            else -> false
        }
        return UploadFailure(
            code = code,
            spoken = spoken,
            retryable = retryable,
            retryAfterSeconds = retryAfter?.takeIf { it >= 0 },
            httpStatus = status,
        )
    }

    private fun defaultCode(status: Int): String = when (status) {
        400 -> "empty_upload"
        401, 403 -> "unauthorized"
        409 -> "attachment_changed"
        413 -> "file_too_large"
        415 -> "unsupported_type"
        422 -> "invalid_media"
        503 -> "relay_unavailable"
        in 500..599 -> "relay_error"
        else -> "upload_failed"
    }

    private fun defaultSpoken(code: String, status: Int): String = when (code) {
        "empty_upload" -> "That file was empty, so there was nothing to save."
        "unauthorized" -> "The relay did not accept the device token."
        "attachment_changed" -> "That file changed after it was saved. Try sending it again."
        "file_too_large", "image_too_large", "archive_too_complex" ->
            "That file is too large to send. Try a smaller one."

        "unsupported_type", "type_mismatch" -> "That file type is not supported."
        "corrupt_media", "corrupt_document", "archive_corrupt" ->
            "That file could not be read. Try another one."

        "document_encrypted" -> "That document is locked with a password."
        "document_too_complex", "parse_timeout" ->
            "That file took too long to read. Try a smaller one."

        "inbox_not_configured" -> "Attachments are not set up on this relay yet."
        "inbox_unavailable", "upload_busy" ->
            "The relay cannot save the file right now. Try again in a moment."

        else -> if (status in 500..599) {
            "The relay had a problem saving the file. Try again."
        } else {
            "The relay could not save the file."
        }
    }

    private fun parseRetryAfter(raw: String?): Long? {
        val value = raw?.trim()?.takeIf { it.isNotEmpty() } ?: return null
        return value.toLongOrNull()?.takeIf { it >= 0 }
    }
}
