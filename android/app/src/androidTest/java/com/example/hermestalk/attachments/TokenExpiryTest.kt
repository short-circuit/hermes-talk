package com.example.hermestalk.attachments

import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.example.hermestalk.rtvi.ConnectionState
import com.example.hermestalk.rtvi.FailureReason
import com.example.hermestalk.rtvi.HttpHealthProbe
import com.example.hermestalk.rtvi.ProbeResult
import com.example.hermestalk.rtvi.RelayEndpoint
import com.example.hermestalk.rtvi.RelaySettings
import com.example.hermestalk.rtvi.RtviSession
import com.example.hermestalk.rtvi.SessionCallbacks
import com.example.hermestalk.rtvi.TlsPolicy
import com.example.hermestalk.rtvi.describe
import com.example.hermestalk.support.LoopbackStubRelay
import java.io.File
import java.util.UUID
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Token expiry across every surface that carries the bearer credential: the
 * liveness probe, the session handshake, the upload engine and the artifact
 * client. A stale token must produce a terminal, explainable failure rather than
 * a retry loop or a crash.
 *
 * The fixture runs on loopback inside the app process; the expired credential is
 * a fixed test string, never a real secret.
 */
@RunWith(AndroidJUnit4::class)
class TokenExpiryTest {

    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context: Context = instrumentation.targetContext.applicationContext

    private lateinit var relay: LoopbackStubRelay
    private lateinit var cacheDir: File
    private var session: RtviSession? = null

    @Before
    fun setUp() {
        relay = LoopbackStubRelay(bearerToken = FRESH_TOKEN)
        cacheDir = File(context.cacheDir, "token-expiry-" + UUID.randomUUID().toString())
        cacheDir.mkdirs()
    }

    @After
    fun tearDown() {
        instrumentation.runOnMainSync { session?.release() }
        session = null
        relay.close()
        cacheDir.deleteRecursively()
    }

    @Test
    fun expiredTokenIsRejectedByTheLivenessProbe() = runBlocking {
        val result = HttpHealthProbe().probe(
            endpoint = endpoint(),
            deviceToken = EXPIRED_TOKEN,
            policy = TlsPolicy.CleartextLoopback,
        )
        assertTrue("expected a failed probe, got $result", result is ProbeResult.Failure)
        val failure = result as ProbeResult.Failure
        assertEquals(FailureReason.Unauthorized, failure.reason)
        assertTrue(
            "the detail must name the status, got '${failure.detail}'",
            failure.detail.startsWith("Unauthorized (HTTP 401)"),
        )
        assertFalse(
            "the device token must never leak into the UI detail",
            failure.detail.contains(EXPIRED_TOKEN),
        )
        assertEquals(1, relay.requestsFor("GET", "/health").size)
    }

    @Test
    fun sessionStartWithExpiredTokenFailsWithoutRetrying() = runBlocking {
        instrumentation.runOnMainSync {
            session = RtviSession(context, callbacks = SessionCallbacks.None)
        }
        val settings = RelaySettings(relayUrl = relay.baseUrl, deviceToken = EXPIRED_TOKEN)
        val state = session!!.connect(settings)

        assertTrue("expected a failed connection, got ${state.describe()}", state is ConnectionState.Failed)
        val failure = state as ConnectionState.Failed
        assertEquals(FailureReason.Unauthorized, failure.reason)
        assertFalse("an expired token must not be retried automatically", failure.retryable)
        assertFalse(
            "the session must not sit in a retry loop",
            session!!.state.value is ConnectionState.Reconnecting,
        )
        assertTrue(
            "the health endpoint must be the only call the relay sees",
            relay.requests().all { it.path == "/health" },
        )
    }

    @Test
    fun uploadWithExpiredTokenIsTerminal() = runBlocking {
        val payloadFile = File(cacheDir, "payload.bin")
        payloadFile.writeBytes(PAYLOAD.toByteArray())
        val payload = UploadPayload(
            file = payloadFile,
            displayName = "note.txt",
            mimeType = "text/plain",
            sizeBytes = payloadFile.length(),
            sha256 = LoopbackStubRelay.sha256Hex(PAYLOAD.toByteArray()),
        )
        val outcome = HttpUploadEngine().upload(
            settings = RelaySettings(relayUrl = relay.baseUrl, deviceToken = EXPIRED_TOKEN),
            payload = payload,
            onProgress = { _, _ -> },
        )

        assertTrue("expected a failed upload, got $outcome", outcome is UploadOutcome.Failure)
        val failure = (outcome as UploadOutcome.Failure).failure
        assertEquals("unauthorized", failure.code)
        assertFalse("a 401 must not be retried", failure.retryable)
        assertEquals(401, failure.httpStatus)
        assertEquals(1, relay.requestsFor("POST", "/v1/attachments").size)
    }

    @Test
    fun artifactListWithExpiredTokenIsReportedNotThrown() = runBlocking {
        val outcome = ArtifactClient(cacheDir).list(
            RelaySettings(relayUrl = relay.baseUrl, deviceToken = EXPIRED_TOKEN),
        )
        assertTrue("expected a failed listing, got $outcome", outcome is ArtifactListOutcome.Failure)
        val failure = outcome as ArtifactListOutcome.Failure
        assertEquals("unauthorized", failure.error.code)
        assertEquals(401, failure.error.httpStatus)
        assertNotNull(failure.error.spoken)
    }

    private fun endpoint(): RelayEndpoint {
        val parsed = RelayEndpoint.parse(relay.baseUrl)
        assertTrue("the fixture URL must parse", parsed is RelayEndpoint.ParseResult.Valid)
        return (parsed as RelayEndpoint.ParseResult.Valid).endpoint
    }

    private companion object {
        /** What the relay accepts. */
        const val FRESH_TOKEN = "fresh-token"

        /** What the app still holds after the token rotated. */
        const val EXPIRED_TOKEN = "expired-token"

        const val PAYLOAD = "expired token payload"
    }
}
