package com.example.hermestalk.e2e

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.example.hermestalk.rtvi.HttpHealthProbe
import com.example.hermestalk.rtvi.ProbeResult
import com.example.hermestalk.rtvi.RelayEndpoint
import com.example.hermestalk.rtvi.TlsPolicy
import java.net.HttpURLConnection
import java.net.URL
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Proves the emulator reaches the containerised relay from `deploy/compose.yaml`.
 *
 * The relay binds the host's loopback, so the supported path is
 * `adb reverse tcp:8790 tcp:8790`; `scripts/relay-container-up.sh` sets that
 * forward up together with the stack. See `docs/DEPLOYMENT.md` ("Emulator
 * access") for the emulator host alias and why the tests do not depend on it.
 *
 * The target URL defaults to `http://127.0.0.1:8790` and can be overridden with
 * `-e relayUrl http://<host>:<port>` so the same test can check another path
 * without a code change. Both tests skip with a clear message when nothing
 * answers on the forwarded port, which keeps the suite green on a bare emulator
 * and in CI.
 */
@RunWith(AndroidJUnit4::class)
class ContainerRelayProbeTest {

    @Test
    fun appProbeReportsTheContainerRelayReachable() = runBlocking {
        val endpoint = endpoint()
        assumeTrue(
            "$RELAY_URL must be forwarded to the container relay (scripts/relay-container-up.sh)",
            reachable(endpoint),
        )

        val result = HttpHealthProbe().probe(endpoint, DEVICE_TOKEN, TlsPolicy.CleartextLoopback)
        assertEquals(
            "the app's own HttpHealthProbe must accept the container relay, got $result",
            ProbeResult.Ok,
            result,
        )
    }

    @Test
    fun containerRelayReportsReadyInsideTheEmulator() = runBlocking {
        val endpoint = endpoint()
        assumeTrue(
            "$RELAY_URL must be forwarded to the container relay (scripts/relay-container-up.sh)",
            reachable(endpoint),
        )

        val response = get(endpoint.baseUrl() + READY_PATH)
        assertEquals(
            "/ready must answer 200 from inside the emulator, body: ${response.body}",
            200,
            response.status,
        )
        val status = JSONObject(response.body).optString("status")
        assertEquals(
            "/ready must report status=ready, body: ${response.body}",
            "ready",
            status,
        )
    }

    private fun endpoint(): RelayEndpoint {
        val parsed = RelayEndpoint.parse(RELAY_URL)
        assumeTrue("$RELAY_URL must parse as a relay endpoint", parsed is RelayEndpoint.ParseResult.Valid)
        return (parsed as RelayEndpoint.ParseResult.Valid).endpoint
    }

    private fun reachable(endpoint: RelayEndpoint): Boolean = runBlocking {
        HttpHealthProbe().probe(endpoint, DEVICE_TOKEN, TlsPolicy.CleartextLoopback) is ProbeResult.Ok
    }

    private data class HttpResponse(val status: Int, val body: String)

    private fun get(url: String): HttpResponse {
        val connection = URL(url).openConnection() as HttpURLConnection
        return try {
            connection.requestMethod = "GET"
            connection.connectTimeout = TIMEOUT_MS
            connection.readTimeout = TIMEOUT_MS
            val status = connection.responseCode
            val stream = if (status in 200..299) connection.inputStream else connection.errorStream
            HttpResponse(status, stream?.bufferedReader()?.use { it.readText() } ?: "")
        } finally {
            connection.disconnect()
        }
    }

    private companion object {
        /** Loopback only: the container relay is reached through `adb reverse`. */
        const val DEFAULT_RELAY_URL = "http://127.0.0.1:8790"

        /** `/health` and `/ready` are unauthenticated, so an empty bearer is fine. */
        const val DEVICE_TOKEN = ""

        const val READY_PATH = "/ready"
        const val TIMEOUT_MS = 5_000

        /** Default target, overridable with `-e relayUrl http://<host>:<port>`. */
        val RELAY_URL: String by lazy {
            InstrumentationRegistry.getArguments().getString("relayUrl")?.trim()
                ?.takeIf { it.isNotEmpty() } ?: DEFAULT_RELAY_URL
        }
    }
}
