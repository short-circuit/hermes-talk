package com.example.hermestalk.attachments

import com.example.hermestalk.rtvi.RelaySettings
import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class UploadQueueControllerTest {

    @get:Rule
    val folder = TemporaryFolder()

    @Test
    fun resumesPersistedWorkAfterProcessDeath() = runBlocking {
        val root = folder.newFolder("attachments")
        val id = "att-resume"
        val payload = File(root, "queue/$id/payload.bin")
        payload.parentFile.mkdirs()
        payload.writeBytes(byteArrayOf(1, 2, 3))
        UploadQueueStore(root).save(
            listOf(
                UploadRecord(
                    id = id,
                    displayName = "resume.png",
                    mimeType = "image/png",
                    sizeBytes = 3,
                    sha256 = "abc",
                    createdAtMillis = 1,
                    status = UploadStatus.UPLOADING,
                ),
            ),
        )

        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        val controller = UploadQueueController(
            rootDir = root,
            engine = FakeTransport(
                UploadOutcome.Success(
                    UploadSuccess("relay-id", "Picture saved.", "abc", 3, "image/png", "image"),
                ),
            ),
            scope = scope,
            delayOverride = { },
        )
        try {
            controller.start(settings())
            val item = withTimeout(5_000) {
                controller.items.first { list ->
                    list.any { it.id == id && it.state is UploadItemState.Sent }
                }.first { it.id == id }
            }
            assertTrue(item.state is UploadItemState.Sent)
            assertEquals("Picture saved.", (item.state as UploadItemState.Sent).spoken)
            assertTrue(!payload.exists())
            assertEquals(UploadStatus.SENT, UploadQueueStore(root).load().first().status)
        } finally {
            controller.close()
            scope.cancel()
        }
    }

    @Test
    fun keepsThePayloadAfterATerminalFailure() = runBlocking {
        val root = folder.newFolder("attachments")
        val id = "att-fail"
        val payload = File(root, "queue/$id/payload.bin")
        payload.parentFile.mkdirs()
        payload.writeBytes(byteArrayOf(4, 5, 6))
        UploadQueueStore(root).save(
            listOf(
                UploadRecord(
                    id = id,
                    displayName = "fail.png",
                    mimeType = "image/png",
                    sizeBytes = 3,
                    sha256 = "abc",
                    createdAtMillis = 1,
                    status = UploadStatus.QUEUED,
                ),
            ),
        )

        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        val controller = UploadQueueController(
            rootDir = root,
            engine = FakeTransport(
                UploadOutcome.Failure(
                    UploadFailure(
                        code = "unsupported_type",
                        spoken = "That file type is not supported.",
                        retryable = false,
                        httpStatus = 415,
                    ),
                ),
            ),
            scope = scope,
            delayOverride = { },
        )
        try {
            controller.start(settings())
            val item = withTimeout(5_000) {
                controller.items.first { list ->
                    list.any { it.id == id && it.state is UploadItemState.Failed }
                }.first { it.id == id }
            }
            assertEquals(
                "That file type is not supported.",
                (item.state as UploadItemState.Failed).spoken,
            )
            assertTrue(payload.exists())
            assertEquals(UploadStatus.FAILED, UploadQueueStore(root).load().first().status)
        } finally {
            controller.close()
            scope.cancel()
        }
    }

    @Test
    fun stopsAfterTheBoundedRetryBudget() = runBlocking {
        val root = folder.newFolder("attachments")
        val id = "att-retry"
        val payload = File(root, "queue/$id/payload.bin")
        payload.parentFile.mkdirs()
        payload.writeBytes(byteArrayOf(7, 8, 9))
        UploadQueueStore(root).save(
            listOf(
                UploadRecord(
                    id = id,
                    displayName = "retry.png",
                    mimeType = "image/png",
                    sizeBytes = 3,
                    sha256 = "abc",
                    createdAtMillis = 1,
                    status = UploadStatus.QUEUED,
                ),
            ),
        )

        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        val controller = UploadQueueController(
            rootDir = root,
            engine = FakeTransport(
                UploadOutcome.Failure(
                    UploadFailure("transport", "The relay could not be reached.", retryable = true),
                ),
            ),
            scope = scope,
            delayOverride = { },
        )
        try {
            controller.start(settings())
            withTimeout(5_000) {
                controller.items.first { list ->
                    list.any { it.id == id && it.state is UploadItemState.Failed }
                }
            }
            assertEquals(RetryPolicy.MAX_ATTEMPTS, UploadQueueStore(root).load().first().attempts)
            assertTrue(payload.exists())
        } finally {
            controller.close()
            scope.cancel()
        }
    }

    private fun settings() = RelaySettings(
        relayUrl = "http://127.0.0.1:8790",
        deviceToken = "token",
    )

    private class FakeTransport(private val outcome: UploadOutcome) : UploadTransport {
        override suspend fun upload(
            settings: RelaySettings,
            payload: UploadPayload,
            onProgress: (uploadedBytes: Long, totalBytes: Long) -> Unit,
        ): UploadOutcome {
            onProgress(payload.sizeBytes, payload.sizeBytes)
            return outcome
        }
    }
}
