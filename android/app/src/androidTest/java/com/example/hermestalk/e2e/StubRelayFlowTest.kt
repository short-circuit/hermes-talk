package com.example.hermestalk.e2e

import android.Manifest
import android.content.Context
import android.media.AudioManager
import android.os.SystemClock
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.junit4.createEmptyComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.lifecycle.Lifecycle
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.rule.GrantPermissionRule
import com.example.hermestalk.MainActivity
import com.example.hermestalk.R
import com.example.hermestalk.call.CallControllers
import com.example.hermestalk.call.CallForegroundService
import com.example.hermestalk.call.CallState
import com.example.hermestalk.call.describe
import com.example.hermestalk.call.isLive
import com.example.hermestalk.rtvi.EncryptedTokenStore
import com.example.hermestalk.rtvi.HttpHealthProbe
import com.example.hermestalk.rtvi.ProbeResult
import com.example.hermestalk.rtvi.RelayEndpoint
import com.example.hermestalk.rtvi.RelaySettings
import com.example.hermestalk.rtvi.TlsPolicy
import com.example.hermestalk.support.LoopbackStubRelay
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * End-to-end emulator flows driven through the real activity.
 *
 * [connectsToTheStubRelayAndCompletesACall] needs the Python stub relay from
 * `android/stub-relay` forwarded with `adb reverse tcp:8790 tcp:8790`; it skips
 * with a clear message when the fixture is not running so the rest of the suite
 * stays green on a bare emulator.
 *
 * [expiredTokenShowsTheUnauthorizedReasonWithoutCrashing] is self-contained: the
 * 401 comes from an in-process loopback fixture, so it also runs in CI.
 */
@RunWith(AndroidJUnit4::class)
class StubRelayFlowTest {

    @get:Rule(order = 0)
    val permissions: GrantPermissionRule = GrantPermissionRule.grant(
        Manifest.permission.RECORD_AUDIO,
        Manifest.permission.POST_NOTIFICATIONS,
    )

    @get:Rule(order = 1)
    val composeRule = createEmptyComposeRule()

    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context: Context = instrumentation.targetContext.applicationContext
    private val audioManager: AudioManager by lazy {
        context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    }

    private var scenario: ActivityScenario<MainActivity>? = null
    private var relay: LoopbackStubRelay? = null

    @Before
    fun setUp() {
        composeRule.waitForIdle()
    }

    @After
    fun tearDown() {
        val controller = CallControllers.get(context)
        if (controller.uiState.value.state.isLive) {
            instrumentation.runOnMainSync { controller.endCall() }
        }
        scenario?.close()
        scenario = null
        relay?.close()
        relay = null
    }

    @Test
    fun connectsToTheStubRelayAndCompletesACall() {
        assumeTrue(
            "the stub relay must answer on 127.0.0.1:8790 (run android/stub-relay and adb reverse)",
            stubRelayHealthy(),
        )
        launchWith(RelaySettings(relayUrl = STUB_RELAY_URL, deviceToken = STUB_TOKEN))
        connect()

        awaitEnabled("start_call", READY_TIMEOUT_MS)

        val controller = CallControllers.get(context)
        composeRule.onNodeWithTag("start_call").performScrollTo().performClick()
        await("the call to become active", CALL_TIMEOUT_MS) {
            controller.uiState.value.state is CallState.Active
        }

        composeRule.onNodeWithTag("call_mute").performClick()
        await("the microphone to be muted", CONTROL_TIMEOUT_MS) {
            controller.uiState.value.controls.muted
        }
        composeRule.onNodeWithTag("call_mute").performClick()
        await("the microphone to be unmuted", CONTROL_TIMEOUT_MS) {
            !controller.uiState.value.controls.muted
        }

        composeRule.onNodeWithTag("call_end").performClick()
        await("the call to end", END_TIMEOUT_MS) {
            !controller.uiState.value.state.isLive
        }
        assertTrue(
            "the call must finish in Ended, got ${controller.uiState.value.state.describe()}",
            controller.uiState.value.state is CallState.Ended,
        )
        await("the foreground service to stop", SERVICE_TIMEOUT_MS) {
            !CallForegroundService.isRunning()
        }
        assertFalse(CallForegroundService.isRunning())
        await("the audio mode to be restored", AUDIO_TIMEOUT_MS) {
            audioManager.mode == AudioManager.MODE_NORMAL
        }
        assertEquals(AudioManager.MODE_NORMAL, audioManager.mode)
    }

    @Test
    fun expiredTokenShowsTheUnauthorizedReasonWithoutCrashing() {
        val expired = LoopbackStubRelay(bearerToken = FRESH_TOKEN)
        relay = expired
        launchWith(RelaySettings(relayUrl = expired.baseUrl, deviceToken = EXPIRED_TOKEN))
        connect()

        val unauthorized = context.getString(R.string.error_reason_unauthorized)
        composeRule.waitUntil(FAILURE_TIMEOUT_MS) {
            composeRule.onAllNodesWithText(unauthorized).fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onAllNodesWithText(unauthorized).fetchSemanticsNodes().let { nodes ->
            assertTrue("the failure card must render the reason", nodes.isNotEmpty())
        }
        composeRule.waitUntil(FAILURE_TIMEOUT_MS) {
            composeRule
                .onAllNodesWithText("Unauthorized (HTTP 401)", substring = true)
                .fetchSemanticsNodes()
                .isNotEmpty()
        }
        assertFalse(
            "an expired token must not be advertised as retryable",
            composeRule
                .onAllNodesWithText(context.getString(R.string.retryable_hint))
                .fetchSemanticsNodes()
                .isNotEmpty(),
        )
        assertEquals(
            "the activity must survive a rejected token",
            Lifecycle.State.RESUMED,
            scenario!!.state,
        )
        assertTrue(
            "the relay must have been probed exactly once",
            expired.requestsFor("GET", "/health").size == 1,
        )
    }

    private fun launchWith(settings: RelaySettings) {
        // The store is seeded before the activity starts so the settings screen
        // renders the loaded values; waiting for the "token saved" confirmation
        // proves the asynchronous load has completed before Connect is tapped.
        EncryptedTokenStore(context).write(settings)
        scenario = ActivityScenario.launch(MainActivity::class.java)
        composeRule.waitUntil(LOAD_TIMEOUT_MS) {
            composeRule
                .onAllNodesWithText(context.getString(R.string.token_saved))
                .fetchSemanticsNodes()
                .isNotEmpty()
        }
    }

    private fun connect() {
        composeRule.onNodeWithTag("settings_connect").performScrollTo().performClick()
    }

    private fun awaitEnabled(tag: String, timeoutMs: Long) {
        composeRule.waitUntil(timeoutMs) {
            runCatching { composeRule.onNodeWithTag(tag).assertIsEnabled() }.isSuccess
        }
    }

    private fun stubRelayHealthy(): Boolean = runBlocking {
        val parsed = RelayEndpoint.parse(STUB_RELAY_URL)
        if (parsed !is RelayEndpoint.ParseResult.Valid) {
            return@runBlocking false
        }
        HttpHealthProbe().probe(parsed.endpoint, "", TlsPolicy.CleartextLoopback) is ProbeResult.Ok
    }

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
        /** Loopback only: the host fixture is reached through `adb reverse`. */
        const val STUB_RELAY_URL = "http://127.0.0.1:8790"
        const val STUB_TOKEN = "stub-token"
        const val FRESH_TOKEN = "fresh-token"
        const val EXPIRED_TOKEN = "expired-token"
        const val LOAD_TIMEOUT_MS = 15_000L
        const val READY_TIMEOUT_MS = 60_000L
        const val CALL_TIMEOUT_MS = 45_000L
        const val FAILURE_TIMEOUT_MS = 30_000L
        const val END_TIMEOUT_MS = 30_000L
        const val CONTROL_TIMEOUT_MS = 10_000L
        const val SERVICE_TIMEOUT_MS = 15_000L
        const val AUDIO_TIMEOUT_MS = 10_000L
        const val POLL_INTERVAL_MS = 100L
    }
}
