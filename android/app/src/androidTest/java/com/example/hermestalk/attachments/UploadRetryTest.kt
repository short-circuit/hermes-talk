package com.example.hermestalk.attachments

import android.content.Context
import android.os.SystemClock
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.example.hermestalk.rtvi.RelaySettings
import com.example.hermestalk.support.LoopbackStubRelay
import java.io.ByteArrayInputStream
import java.io.File
import java.util.UUID
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Upload retry behaviour on a real device: the queue is driven against a
 * loopback HTTP fixture inside the app process, so the multipart framing,
 * `HttpURLConnection` timeouts, retry schedule and durable queue all run for
 * real. No host relay and no `adb reverse` are required.
 */
@RunWith(AndroidJUnit4::class)
class UploadRetryTest {

    private val context: Context =
        InstrumentationRegistry.getInstrumentation().targetContext.applicationContext

    private lateinit var relay: LoopbackStubRelay
    private lateinit var rootDir: File
    private lateinit var scope: CoroutineScope
    private var controller: UploadQueueController? = null
    private val retryDelays = mutableListOf<Long>()

    @Before
    fun setUp() {
        relay = LoopbackStubRelay()
        rootDir = File(context.cacheDir, "upload-retry-" + UUID.randomUUID().toString())
        scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    }

    @After
    fun tearDown() {
        controller?.close()
        controller = null
        scope.cancel()
        relay.close()
        rootDir.deleteRecursively()
    }

    @Test
    fun transientRelayFailuresAreRetriedAndTheUploadCompletes() {
        relay.enqueueUploadStatuses(503, 503)
        val settings = RelaySettings(relayUrl = relay.baseUrl, deviceToken = TOKEN)
        val queue = newController(settings)
        val item = queue.ingestAndEnqueue(
            source = ByteArrayInputStream(PAYLOAD.toByteArray()),
            displayName = "note.txt",
            mimeType = "text/plain",
            settings = settings,
        )
        assertNotNull("the payload must be queued", item)

        await("upload to be sent", SEND_TIMEOUT_MS) {
            queue.items.value.firstOrNull { it.id == item!!.id }?.state is UploadItemState.Sent
        }

        val uploads = relay.requestsFor("POST", "/v1/attachments")
        assertEquals("two transient failures then a success", 3, uploads.size)
        assertTrue(
            "every attempt must carry the bearer token",
            uploads.all { it.bearerToken == TOKEN },
        )
        assertTrue(
            "the payload must reach the relay unchanged",
            uploads.all { it.bodyContains(PAYLOAD) },
        )
        assertTrue(
            "the declared content length must match the body the fixture read",
            uploads.all { request ->
                request.headers["content-length"]?.toLongOrNull() == request.body.size.toLong()
            },
        )
        assertEquals(
            "the retry schedule must back off 1 s then 2 s",
            listOf(1_000L, 2_000L),
            retryDelays,
        )
        assertFalse(
            "the persisted payload must be removed after a 201",
            queuePayload(item!!.id).exists(),
        )
    }

    @Test
    fun unauthorizedUploadIsTerminalAndKeepsThePayloadForManualRetry() {
        relay.close()
        relay = LoopbackStubRelay(bearerToken = "expected-token")
        val settings = RelaySettings(relayUrl = relay.baseUrl, deviceToken = "wrong-token")
        val queue = newController(settings)
        val item = queue.ingestAndEnqueue(
            source = ByteArrayInputStream(PAYLOAD.toByteArray()),
            displayName = "note.txt",
            mimeType = "text/plain",
            settings = settings,
        )
        assertNotNull(item)

        await("upload to fail", SEND_TIMEOUT_MS) {
            queue.items.value.firstOrNull { it.id == item!!.id }?.state is UploadItemState.Failed
        }

        val state = queue.items.value.first { it.id == item!!.id }.state
        assertTrue("expected a failed upload, got $state", state is UploadItemState.Failed)
        assertEquals("unauthorized", (state as UploadItemState.Failed).code)
        assertEquals(
            "a 401 must not be retried automatically",
            1,
            relay.requestsFor("POST", "/v1/attachments").size,
        )
        assertTrue(
            "the payload must survive so the user can retry after fixing the token",
            queuePayload(item!!.id).exists(),
        )
        assertEquals(
            "the visible error must explain the rejection",
            "The relay did not accept the device token.",
            queue.error.value,
        )
    }

    @Test
    fun queuedUploadResumesAfterProcessRestart() {
        val settings = RelaySettings(relayUrl = relay.baseUrl, deviceToken = TOKEN)
        val id = "att-resume"
        val payloadFile = File(rootDir, "queue/$id/payload.bin")
        payloadFile.parentFile?.mkdirs()
        payloadFile.writeBytes(PAYLOAD.toByteArray())
        val store = UploadQueueStore(rootDir)
        store.save(
            listOf(
                UploadRecord(
                    id = id,
                    displayName = "resumed.txt",
                    mimeType = "text/plain",
                    sizeBytes = payloadFile.length(),
                    sha256 = LoopbackStubRelay.sha256Hex(PAYLOAD.toByteArray()),
                    createdAtMillis = 1_700_000_000_000L,
                    status = UploadStatus.QUEUED,
                ),
            ),
        )

        // A fresh controller over the same directory is what the app does on
        // process start: it loads the persisted queue and resumes it.
        val queue = newController(settings)
        queue.start(settings)

        await("resumed upload to be sent", SEND_TIMEOUT_MS) {
            queue.items.value.firstOrNull { it.id == id }?.state is UploadItemState.Sent
        }
        assertEquals(1, relay.requestsFor("POST", "/v1/attachments").size)
        assertFalse(payloadFile.exists())
    }

    private fun newController(settings: RelaySettings): UploadQueueController {
        val created = UploadQueueController(
            rootDir = rootDir,
            engine = HttpUploadEngine(),
            connectivity = NoopConnectivityMonitor(),
            scope = scope,
            delayOverride = { millis -> retryDelays += millis },
        )
        controller = created
        created.updateSettings(settings)
        return created
    }

    private fun queuePayload(id: String): File = File(rootDir, "queue/$id/payload.bin")

    private fun await(label: String, timeoutMs: Long, condition: () -> Boolean) {
        val deadline = SystemClock.uptimeMillis() + timeoutMs
        while (SystemClock.uptimeMillis() < deadline) {
            if (condition()) {
                return
            }
            SystemClock.sleep(POLL_INTERVAL_MS)
        }
        fail("timed out waiting for $label")
    }

    private companion object {
        const val TOKEN = "stub-token"
        const val PAYLOAD = "retry payload bytes"
        const val SEND_TIMEOUT_MS = 20_000L
        const val POLL_INTERVAL_MS = 50L
    }
}
