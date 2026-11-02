package com.example.hermestalk.attachments

import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.io.InputStream
import java.security.MessageDigest

/** Maximum payload the phone will persist for one upload. */
const val MAX_ATTACHMENT_BYTES: Long = 50L * 1024L * 1024L

/** Result of copying a shared stream into the durable queue. */
sealed interface IngestOutcome {
    data class Success(val record: UploadRecord, val payloadFile: File) : IngestOutcome

    data class Failure(val code: String, val spoken: String) : IngestOutcome
}

/**
 * Sanitize a display name to a basename. Path separators on both POSIX and
 * Windows, control characters and a runaway length are handled here; no I/O is
 * performed.
 */
object AttachmentNames {

    const val MAX_LENGTH = 120
    const val FALLBACK = "attachment"

    fun sanitize(raw: String?): String {
        val value = raw?.trim().orEmpty()
        if (value.isEmpty()) {
            return FALLBACK
        }
        val basename = value.replace('\\', '/').substringAfterLast('/')
        val cleaned = buildString {
            for (character in basename) {
                when {
                    character == '/' || character == '\\' -> append('_')
                    character.code < 0x20 || character.code == 0x7f -> append('_')
                    else -> append(character)
                }
            }
        }.trim().trim('.').ifEmpty { FALLBACK }
        return cleaned.take(MAX_LENGTH).trim().trim('.').ifEmpty { FALLBACK }
    }

    /**
     * The relay requires an extension that belongs to the sniffed type family.
     * Keep the original extension when present; otherwise add a canonical one
     * derived from the declared MIME type.
     */
    fun withExtension(name: String, mimeType: String?): String {
        if (name.substringAfterLast('/').contains('.')) {
            return name
        }
        val extension = extensionFor(mimeType) ?: return name
        return "$name.$extension"
    }

    private fun extensionFor(mimeType: String?): String? = when (mimeType?.lowercase()) {
        "image/png" -> "png"
        "image/jpeg", "image/jpg" -> "jpg"
        "image/webp" -> "webp"
        "image/gif" -> "gif"
        "application/pdf" -> "pdf"
        "application/zip" -> "zip"
        "audio/wav", "audio/x-wav" -> "wav"
        "audio/mpeg" -> "mp3"
        "audio/ogg" -> "ogg"
        "audio/flac", "audio/x-flac" -> "flac"
        "audio/mp4" -> "m4a"
        "audio/webm" -> "webm"
        "text/plain", "text/markdown", "text/csv" -> "txt"
        else -> null
    }
}

/**
 * Streams one shared file into `queue/<id>/payload.bin`, computing its SHA-256
 * and size. The class is framework-free so the JVM tests can exercise the cap,
 * empty-file rejection and hashing directly.
 */
class AttachmentIngest(private val rootDir: File) {

    fun payloadFile(id: String): File = File(queueDir(), "$id/payload.bin")

    fun ingest(
        source: InputStream,
        displayName: String?,
        mimeType: String?,
        id: String,
        createdAtMillis: Long,
    ): IngestOutcome {
        val sanitized = AttachmentNames.withExtension(
            AttachmentNames.sanitize(displayName),
            mimeType,
        )
        val recordDir = File(queueDir(), id)
        if (!recordDir.exists() && !recordDir.mkdirs()) {
            return IngestOutcome.Failure("storage_unavailable", "The phone could not save the file.")
        }
        val payload = File(recordDir, "payload.bin")
        val partial = File(recordDir, "payload.bin.part")
        val digest = MessageDigest.getInstance("SHA-256")
        var total = 0L
        try {
            FileOutputStream(partial).use { output ->
                val buffer = ByteArray(BUFFER_BYTES)
                while (true) {
                    val read = source.read(buffer)
                    if (read < 0) {
                        break
                    }
                    if (read == 0) {
                        continue
                    }
                    total += read
                    if (total > MAX_ATTACHMENT_BYTES) {
                        return IngestOutcome.Failure(
                            "file_too_large",
                            "That file is too large to send. Try a smaller one.",
                        ).also {
                            partial.delete()
                        }
                    }
                    digest.update(buffer, 0, read)
                    output.write(buffer, 0, read)
                }
                output.fd.sync()
            }
            if (total == 0L) {
                partial.delete()
                return IngestOutcome.Failure(
                    "empty_upload",
                    "That file was empty, so there was nothing to save.",
                )
            }
            if (!partial.renameTo(payload)) {
                partial.delete()
                return IngestOutcome.Failure(
                    "storage_unavailable",
                    "The phone could not save the file.",
                )
            }
            return IngestOutcome.Success(
                UploadRecord(
                    id = id,
                    displayName = sanitized,
                    mimeType = mimeType,
                    sizeBytes = total,
                    sha256 = digest.digest().toHex(),
                    createdAtMillis = createdAtMillis,
                    status = UploadStatus.QUEUED,
                ),
                payload,
            )
        } catch (failure: IOException) {
            partial.delete()
            payload.delete()
            return IngestOutcome.Failure(
                "read_failed",
                "The phone could not read that file.",
            )
        } finally {
            runCatching { source.close() }
        }
    }

    private fun queueDir(): File = File(rootDir, "queue").also { it.mkdirs() }

    private companion object {
        const val BUFFER_BYTES = 64 * 1024
    }
}

internal fun ByteArray.toHex(): String = joinToString("") { byte ->
    "%02x".format(byte)
}
