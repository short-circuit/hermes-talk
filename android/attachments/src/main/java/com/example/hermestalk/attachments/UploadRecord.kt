package com.example.hermestalk.attachments

import kotlinx.serialization.Serializable

/**
 * Durable upload record. It contains no credential: the device token stays in
 * the encrypted settings store and is only put on the Authorization header.
 */
@Serializable
data class UploadRecord(
    val id: String,
    val displayName: String,
    val mimeType: String? = null,
    val sizeBytes: Long = 0,
    val sha256: String = "",
    val createdAtMillis: Long = 0,
    val status: String = UploadStatus.QUEUED,
    val attempts: Int = 0,
    val spoken: String? = null,
    val failureCode: String? = null,
    val failureSpoken: String? = null,
)

/** Persisted status strings. Unknown values are treated as queued on load. */
object UploadStatus {
    const val QUEUED = "queued"
    const val UPLOADING = "uploading"
    const val RETRYING = "retrying"
    const val SENT = "sent"
    const val FAILED = "failed"
    const val CANCELLED = "cancelled"

    fun isCompleted(status: String): Boolean = status == SENT || status == CANCELLED

    fun isResumable(status: String): Boolean =
        status == QUEUED || status == UPLOADING || status == RETRYING
}

/** Observable state for one queued upload. */
sealed interface UploadItemState {
    data object Queued : UploadItemState

    data class Uploading(val progress: Float) : UploadItemState

    data class Retrying(val attempt: Int, val delaySeconds: Long) : UploadItemState

    data class Sent(val spoken: String) : UploadItemState

    data class Failed(val spoken: String, val code: String? = null) : UploadItemState

    data object Cancelled : UploadItemState
}

/** UI-facing upload item. The payload path is derived from [id]. */
data class UploadItem(
    val id: String,
    val displayName: String,
    val mimeType: String?,
    val sizeBytes: Long,
    val sha256: String,
    val createdAtMillis: Long,
    val state: UploadItemState,
)

internal fun UploadRecord.toItem(): UploadItem = UploadItem(
    id = id,
    displayName = displayName,
    mimeType = mimeType,
    sizeBytes = sizeBytes,
    sha256 = sha256,
    createdAtMillis = createdAtMillis,
    state = when (status) {
        UploadStatus.SENT -> UploadItemState.Sent(spoken ?: "File saved.")
        UploadStatus.FAILED -> UploadItemState.Failed(
            failureSpoken ?: "The relay could not save the file.",
            failureCode,
        )

        UploadStatus.CANCELLED -> UploadItemState.Cancelled
        UploadStatus.RETRYING -> UploadItemState.Queued
        UploadStatus.UPLOADING -> UploadItemState.Queued
        else -> UploadItemState.Queued
    },
)
