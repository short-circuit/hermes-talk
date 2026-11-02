package com.example.hermestalk.attachments

/**
 * Fixed-length multipart framing for the relay's single `file` field.
 *
 * The relay accepts `multipart/form-data` with one field named `file`; the
 * filename's extension must match the sniffed content type. Framing is built
 * here rather than by a streaming multipart library so the exact
 * `Content-Length` is known before the request starts.
 */
class MultipartBody(
    val boundary: String,
    filename: String,
    contentType: String?,
    val payloadSize: Long,
) {

    private val safeFilename = headerSafe(filename)
    private val safeContentType = contentType
        ?.takeIf { it.isNotBlank() && it.none { character -> character == '\r' || character == '\n' } }
        ?: "application/octet-stream"

    val prefix: ByteArray = buildString {
        append("--").append(boundary).append("\r\n")
        append("Content-Disposition: form-data; name=\"file\"; filename=\"")
            .append(safeFilename)
            .append("\"\r\n")
        append("Content-Type: ").append(safeContentType).append("\r\n")
        append("\r\n")
    }.toByteArray(Charsets.UTF_8)

    val suffix: ByteArray = buildString {
        append("\r\n--").append(boundary).append("--\r\n")
    }.toByteArray(Charsets.UTF_8)

    val contentLength: Long
        get() = prefix.size.toLong() + payloadSize + suffix.size.toLong()

    private fun headerSafe(raw: String): String {
        val fallback = "attachment"
        val value = raw.trim().ifEmpty { fallback }
        return buildString {
            for (character in value) {
                when {
                    character == '"' || character == '\\' -> append('_')
                    character.code < 0x20 || character.code == 0x7f -> append('_')
                    character.code > 0x7e -> append('_')
                    else -> append(character)
                }
            }
        }.ifEmpty { fallback }
    }
}
