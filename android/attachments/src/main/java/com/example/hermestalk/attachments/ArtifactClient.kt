package com.example.hermestalk.attachments

import com.example.hermestalk.rtvi.RelayEndpoint
import com.example.hermestalk.rtvi.RelaySettings
import com.example.hermestalk.rtvi.TlsPolicy
import com.example.hermestalk.rtvi.TlsTrust
import com.example.hermestalk.rtvi.tlsPolicy
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import javax.net.ssl.HttpsURLConnection
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.Serializable
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json

/** One artifact returned by `GET /v1/artifacts`. */
data class ArtifactInfo(
    val id: String,
    val name: String,
    val mime: String,
    val sizeBytes: Long,
    val sha256: String,
    val createdAt: String? = null,
    val expiresAt: String? = null,
)

data class ArtifactList(
    val artifacts: List<ArtifactInfo>,
    val count: Int,
    val limit: Int,
    val retentionDays: Int?,
)

data class ArtifactError(
    val code: String,
    val spoken: String,
    val httpStatus: Int,
)

sealed interface ArtifactListOutcome {
    data class Success(val list: ArtifactList) : ArtifactListOutcome

    data class Failure(val error: ArtifactError) : ArtifactListOutcome
}

sealed interface ArtifactDownloadOutcome {
    data class Success(val file: File, val mime: String, val sha256: String) :
        ArtifactDownloadOutcome

    data class Failure(val error: ArtifactError) : ArtifactDownloadOutcome
}

sealed interface ArtifactParseResult {
    data class Success(val list: ArtifactList) : ArtifactParseResult

    data class Invalid(val reason: String) : ArtifactParseResult
}

@Serializable
internal data class ArtifactListResponse(
    val artifacts: List<ArtifactDto> = emptyList(),
    val count: Int? = null,
    val limit: Int? = null,
    val retention_days: Int? = null,
)

@Serializable
internal data class ArtifactDto(
    val id: String? = null,
    val name: String? = null,
    val mime: String? = null,
    val size_bytes: Long? = null,
    val sha256: String? = null,
    val created_at: String? = null,
    val expires_at: String? = null,
)

@Serializable
internal data class ArtifactErrorBody(
    val error: String? = null,
    val detail: String? = null,
    val spoken: String? = null,
)

internal object ArtifactJson {
    val json = Json {
        ignoreUnknownKeys = true
        encodeDefaults = true
    }

    fun parseList(body: String): ArtifactParseResult {
        val response = try {
            json.decodeFromString<ArtifactListResponse>(body)
        } catch (failure: Exception) {
            return ArtifactParseResult.Invalid("The relay returned an invalid artifact list.")
        }
        val artifacts = response.artifacts.map { dto ->
            val id = dto.id?.trim()?.takeIf { it.isNotEmpty() }
                ?: return ArtifactParseResult.Invalid("An artifact is missing its id.")
            val name = dto.name?.trim()?.takeIf { it.isNotEmpty() }
                ?: return ArtifactParseResult.Invalid("An artifact is missing its name.")
            val mime = dto.mime?.trim()?.takeIf { it.isNotEmpty() }
                ?: return ArtifactParseResult.Invalid("An artifact is missing its MIME type.")
            val size = dto.size_bytes
                ?: return ArtifactParseResult.Invalid("An artifact is missing its size.")
            val sha = dto.sha256?.trim()?.takeIf { it.isNotEmpty() }
                ?: return ArtifactParseResult.Invalid("An artifact is missing its checksum.")
            ArtifactInfo(
                id = id,
                name = name,
                mime = mime,
                sizeBytes = size,
                sha256 = sha,
                createdAt = dto.created_at,
                expiresAt = dto.expires_at,
            )
        }
        return ArtifactParseResult.Success(
            ArtifactList(
                artifacts = artifacts,
                count = response.count ?: artifacts.size,
                limit = response.limit ?: 50,
                retentionDays = response.retention_days,
            ),
        )
    }

    fun parseError(body: String?): ArtifactErrorBody? = runCatching {
        json.decodeFromString<ArtifactErrorBody>(body.orEmpty())
    }.getOrNull()
}

/** Pure URL helpers. Artifact ids are relative POSIX paths. */
object ArtifactUrl {

    fun listUrl(endpoint: RelayEndpoint, limit: Int = 50): String =
        endpoint.baseUrl() + "/v1/artifacts?limit=$limit"

    fun downloadUrl(endpoint: RelayEndpoint, artifactId: String): String =
        endpoint.baseUrl() + "/v1/artifacts/" + encodeId(artifactId)

    /** Percent-encode each path segment, leaving `/` as the separator. */
    fun encodeId(artifactId: String): String =
        artifactId.split('/').joinToString("/") { encodeSegment(it) }

    private fun encodeSegment(segment: String): String {
        val bytes = segment.toByteArray(Charsets.UTF_8)
        return buildString {
            for (byte in bytes) {
                val value = byte.toInt() and 0xff
                val character = value.toChar()
                if (
                    character in 'A'..'Z' ||
                    character in 'a'..'z' ||
                    character in '0'..'9' ||
                    character == '-' || character == '.' ||
                    character == '_' || character == '~'
                ) {
                    append(character)
                } else {
                    append('%')
                    append(HEX[value ushr 4])
                    append(HEX[value and 0x0f])
                }
            }
        }
    }

    private const val HEX = "0123456789ABCDEF"
}

/**
 * Authenticated artifact listing and download. It uses the same TLS policy and
 * bearer header as [HttpUploadEngine]. Downloads are streamed to
 * `cacheDir/artifacts/` and verified against `X-Artifact-SHA256`; a mismatch
 * deletes the partial file and fails.
 */
class ArtifactClient(
    private val cacheDir: File,
    private val connectTimeoutMs: Int = 15_000,
    private val readTimeoutMs: Int = 60_000,
) {

    suspend fun list(settings: RelaySettings): ArtifactListOutcome = withContext(Dispatchers.IO) {
        val endpoint = parseEndpoint(settings) ?: return@withContext ArtifactListOutcome.Failure(
            ArtifactError("invalid_settings", "The relay address is not valid.", 0),
        )
        if (settings.deviceToken.isBlank()) {
            return@withContext ArtifactListOutcome.Failure(
                ArtifactError("missing_token", "No device token is configured.", 0),
            )
        }

        var connection: HttpURLConnection? = null
        try {
            connection = open(endpoint, ArtifactUrl.listUrl(endpoint), settings)
            val status = connection.responseCode
            val body = readBody(connection, status)
            if (status in 200..299) {
                when (val parsed = ArtifactJson.parseList(body)) {
                    is ArtifactParseResult.Success -> ArtifactListOutcome.Success(parsed.list)
                    is ArtifactParseResult.Invalid -> ArtifactListOutcome.Failure(
                        ArtifactError("invalid_response", parsed.reason, status),
                    )
                }
            } else {
                ArtifactListOutcome.Failure(errorFor(status, body))
            }
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (error: IOException) {
            ArtifactListOutcome.Failure(
                ArtifactError("transport", "The relay could not be reached.", 0),
            )
        } finally {
            connection?.disconnect()
        }
    }

    suspend fun download(
        settings: RelaySettings,
        artifact: ArtifactInfo,
        onProgress: (downloadedBytes: Long, totalBytes: Long) -> Unit,
    ): ArtifactDownloadOutcome = withContext(Dispatchers.IO) {
        val endpoint = parseEndpoint(settings) ?: return@withContext ArtifactDownloadOutcome.Failure(
            ArtifactError("invalid_settings", "The relay address is not valid.", 0),
        )
        if (settings.deviceToken.isBlank()) {
            return@withContext ArtifactDownloadOutcome.Failure(
                ArtifactError("missing_token", "No device token is configured.", 0),
            )
        }

        val directory = File(cacheDir, "artifacts")
        if (!directory.exists() && !directory.mkdirs()) {
            return@withContext ArtifactDownloadOutcome.Failure(
                ArtifactError("storage_unavailable", "The phone could not save the file.", 0),
            )
        }
        val target = File(directory, localName(artifact))
        val partial = File(directory, ".${target.name}.part")
        var connection: HttpURLConnection? = null
        try {
            connection = open(endpoint, ArtifactUrl.downloadUrl(endpoint, artifact.id), settings)
            val status = connection.responseCode
            if (status !in 200..299) {
                val body = readBody(connection, status)
                return@withContext ArtifactDownloadOutcome.Failure(errorFor(status, body))
            }

            val expectedSha = connection.getHeaderField("X-Artifact-SHA256")
                ?.trim()
                ?.takeIf { it.isNotEmpty() }
                ?: artifact.sha256
            val contentLength = connection.contentLengthLong.takeIf { it >= 0 }
                ?: artifact.sizeBytes
            val digest = MessageDigest.getInstance("SHA-256")
            var downloaded = 0L
            var lastReported = 0L
            FileOutputStream(partial).use { output ->
                connection.inputStream.use { input ->
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
                        digest.update(buffer, 0, read)
                        downloaded += read
                        if (downloaded - lastReported >= PROGRESS_INTERVAL_BYTES) {
                            lastReported = downloaded
                            onProgress(downloaded, contentLength)
                        }
                    }
                }
                output.fd.sync()
            }
            onProgress(downloaded, contentLength)
            val observed = digest.digest().toHex()
            if (!observed.equals(expectedSha, ignoreCase = true)) {
                partial.delete()
                return@withContext ArtifactDownloadOutcome.Failure(
                    ArtifactError(
                        "sha256_mismatch",
                        "The downloaded file did not match its checksum.",
                        status,
                    ),
                )
            }
            if (!partial.renameTo(target)) {
                partial.delete()
                return@withContext ArtifactDownloadOutcome.Failure(
                    ArtifactError("storage_unavailable", "The phone could not save the file.", status),
                )
            }
            ArtifactDownloadOutcome.Success(target, artifact.mime, observed)
        } catch (cancelled: CancellationException) {
            partial.delete()
            throw cancelled
        } catch (error: IOException) {
            partial.delete()
            ArtifactDownloadOutcome.Failure(
                ArtifactError("transport", "The relay could not be reached.", 0),
            )
        } finally {
            connection?.disconnect()
        }
    }

    private fun open(
        endpoint: RelayEndpoint,
        url: String,
        settings: RelaySettings,
    ): HttpURLConnection {
        val connection = URL(url).openConnection() as HttpURLConnection
        if (settings.tlsPolicy() is TlsPolicy.Pinned) {
            val policy = settings.tlsPolicy() as TlsPolicy.Pinned
            (connection as? HttpsURLConnection)?.sslSocketFactory =
                TlsTrust.socketFactoryForPin(policy.pin)
        }
        connection.requestMethod = "GET"
        connection.useCaches = false
        connection.connectTimeout = connectTimeoutMs
        connection.readTimeout = readTimeoutMs
        connection.setRequestProperty("Authorization", "Bearer ${settings.deviceToken}")
        return connection
    }

    private fun parseEndpoint(settings: RelaySettings): RelayEndpoint? =
        (RelayEndpoint.parse(settings.relayUrl) as? RelayEndpoint.ParseResult.Valid)?.endpoint

    private fun readBody(connection: HttpURLConnection, status: Int): String {
        val stream = if (status in 200..299) connection.inputStream else connection.errorStream
        return stream?.bufferedReader(Charsets.UTF_8)?.use { reader ->
            val buffer = CharArray(MAX_BODY_CHARS)
            val read = reader.read(buffer)
            if (read <= 0) "" else String(buffer, 0, read)
        }.orEmpty()
    }

    private fun errorFor(status: Int, body: String?): ArtifactError {
        val parsed = ArtifactJson.parseError(body)
        val code = parsed?.error?.trim()?.takeIf { it.isNotEmpty() } ?: when (status) {
            401, 403 -> "unauthorized"
            404 -> "artifact_not_found"
            503 -> "outbox_unavailable"
            else -> "artifact_error"
        }
        val spoken = parsed?.spoken?.trim()?.takeIf { it.isNotEmpty() } ?: when (code) {
            "artifact_not_found" -> "That artifact could not be found."
            "artifact_expired" -> "That artifact has expired."
            "outbox_not_configured" -> "Artifacts are not set up on this relay yet."
            "outbox_unavailable" -> "The relay cannot read artifacts right now."
            "unauthorized" -> "The relay did not accept the device token."
            else -> "The artifact could not be fetched."
        }
        return ArtifactError(code, spoken, status)
    }

    private fun localName(artifact: ArtifactInfo): String {
        val safe = AttachmentNames.sanitize(artifact.name)
        val prefix = artifact.sha256.take(8).ifEmpty { "artifact" }
        return "$prefix-$safe"
    }

    private companion object {
        const val CHUNK_BYTES = 64 * 1024
        const val PROGRESS_INTERVAL_BYTES = 64L * 1024L
        const val MAX_BODY_CHARS = 64 * 1024
    }
}
