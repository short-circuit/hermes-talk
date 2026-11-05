package com.example.hermestalk.e2e

import android.Manifest
import android.content.Context
import android.os.SystemClock
import android.util.Log
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.junit4.createEmptyComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.rule.GrantPermissionRule
import com.example.hermestalk.MainActivity
import com.example.hermestalk.R
import com.example.hermestalk.call.CallControllers
import com.example.hermestalk.call.CallState
import com.example.hermestalk.call.isLive
import com.example.hermestalk.rtvi.CallAudioSources
import com.example.hermestalk.rtvi.CallTrace
import com.example.hermestalk.rtvi.EncryptedTokenStore
import com.example.hermestalk.rtvi.FixtureAudioSource
import com.example.hermestalk.rtvi.RelaySettings
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.net.HttpURLConnection
import java.net.URL

/**
 * Drives the real app against the containerised relay from `deploy/compose.yaml`.
 *
 * The flow is the production one: the activity connects over SmallWebRTC to
 * `http://127.0.0.1:8790` (forwarded into the emulator with `adb reverse`), the
 * relay runs its own VAD, endpointing, STT, brain, and Kokoro TTS, and the app
 * records every event it observes in [CallTrace]. Nothing is stubbed between the
 * app and the relay: there is no fixture server in this test.
 *
 * The emulator microphone cannot be scripted, so the outgoing audio comes from
 * [FixtureAudioSource]: 16 kHz mono PCM16 read from a bundled WAV and sent to the
 * relay as RTVI client messages. The transport, the relay, and every model stay
 * real; only the origin of the microphone samples is scripted. See
 * `docs/TESTING.md` ("Scripted-microphone E2E call").
 *
 * Unlike the probe test this test FAILS when the container is absent: a skipped
 * call test would claim coverage it never had.
 *
 * Run it with `scripts/e2e-emulator-container.sh`, which brings the stack up,
 * forwards the port, issues a device token, and runs exactly this class.
 */
@RunWith(AndroidJUnit4::class)
@LargeTest
class ContainerCallE2ETest {

    @get:Rule(order = 0)
    val permissions: GrantPermissionRule = GrantPermissionRule.grant(
        Manifest.permission.RECORD_AUDIO,
        Manifest.permission.POST_NOTIFICATIONS,
    )

    @get:Rule(order = 1)
    val composeRule = createEmptyComposeRule()

    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context: Context = instrumentation.targetContext.applicationContext
    private val testContext: Context = instrumentation.context

    private var scenario: ActivityScenario<MainActivity>? = null
    private var fixture: FixtureAudioSource? = null

    @Before
    fun setUp() {
        CallTrace.reset()
        CallAudioSources.clear()
        fixture = FixtureAudioSource(testContext, FIXTURE_ASSET)
        CallAudioSources.scripted = fixture
    }

    @After
    fun tearDown() {
        val controller = CallControllers.get(context)
        if (controller.uiState.value.state.isLive) {
            instrumentation.runOnMainSync { controller.endCall() }
        }
        scenario?.close()
        scenario = null
        CallAudioSources.clear()
        fixture = null
    }

    @Test
    fun callThroughTheAppReachesTranscriptAndSpokenAnswer() {
        val token = deviceToken()
        if (token.isEmpty()) {
            fail(
                "no device token: pass -Pandroid.testInstrumentationRunnerArguments.deviceToken=<token>; " +
                    "scripts/e2e-emulator-container.sh issues one",
            )
        }
        if (!containerRelayReady()) {
            fail(
                "the container relay must answer $RELAY_URL/ready with status=ready; " +
                    "start it with scripts/relay-container-up.sh (a stub relay on the same port does not count)",
            )
        }

        launchWith(RelaySettings(relayUrl = RELAY_URL, deviceToken = token))
        connect()

        awaitEnabled("start_call", READY_TIMEOUT_MS)
        val controller = CallControllers.get(context)
        composeRule.onNodeWithTag("start_call").performScrollTo().performClick()
        await("the call to become active", CALL_TIMEOUT_MS) {
            controller.uiState.value.state is CallState.Active
        }

        await("the scripted utterance to be sent", FEED_TIMEOUT_MS) {
            CallTrace.last("audio.feed.end") != null
        }
        val feedEndMaybe = CallTrace.last("audio.feed.end")
        assertNotNull("the fixture must report the end of the utterance", feedEndMaybe)
        val feedEnd = feedEndMaybe!!
        val frames = feedEnd.detail["frames"]?.toIntOrNull() ?: 0
        assertEquals(
            "the call session must have received the whole fixture, frame by frame",
            fixture!!.totalFrames,
            frames,
        )

        // Latency is anchored where the spoken words stop, not where the file's
        // silence tail stops: the relay legitimately endpointed before the tail
        // finished streaming.
        val speechEndMaybe = CallTrace.last("audio.feed.speech.end")
        assertNotNull("the fixture must mark the end of the spoken words", speechEndMaybe)
        val speechEnd = speechEndMaybe!!
        assertTrue(
            "the speech marker must precede the end of the file, got ${speechEnd.millisSince(feedEnd)}ms",
            speechEnd.millisSince(feedEnd) < 0,
        )

        await("a final transcript from the relay", TRANSCRIPT_TIMEOUT_MS) {
            finalTranscript() != null
        }
        val transcriptMaybe = finalTranscript()
        assertNotNull("the relay must return a final user transcript", transcriptMaybe)
        val transcript = transcriptMaybe!!
        val transcriptText = transcript.detail["text"].orEmpty()
        assertTrue(
            "the transcript must contain the spoken words, got '$transcriptText'",
            transcriptText.lowercase().contains(EXPECTED_WORD),
        )

        await("the brain's reply text", REPLY_TIMEOUT_MS) {
            CallTrace.first("bot.llm.text") != null
        }
        val replyMaybe = CallTrace.first("bot.llm.text")
        assertNotNull("the brain must decide what to say", replyMaybe)
        val reply = replyMaybe!!
        val replyText = reply.detail["text"].orEmpty()
        assertTrue("the brain's reply must not be empty", replyText.isNotBlank())

        await("the relay to start speaking", TTS_TIMEOUT_MS) {
            CallTrace.first("bot.tts.started") != null
        }
        await("the relay to report the pushed speech audio", TTS_TIMEOUT_MS) {
            CallTrace.first("bot.audio.stats") != null
        }
        val statsMaybe = CallTrace.first("bot.audio.stats")
        assertNotNull("the relay must report the speech audio it pushed", statsMaybe)
        val stats = statsMaybe!!
        val audioBytes = stats.detail["audio_bytes"]?.toLongOrNull() ?: 0L
        val audioMs = stats.detail["audio_ms"]?.toDoubleOrNull() ?: 0.0
        assertTrue("the relay must push TTS audio, got $audioBytes bytes", audioBytes > 0)
        assertTrue("the reported speech duration must be positive, got $audioMs ms", audioMs > 0.0)

        await("the relay to stop speaking", TTS_TIMEOUT_MS) {
            CallTrace.first("bot.tts.stopped") != null
        }

        val ttsStartedMaybe = CallTrace.first("bot.tts.started")
        assertNotNull("the relay must report that it started speaking", ttsStartedMaybe)
        val ttsStarted = ttsStartedMaybe!!
        val transcriptMs = transcript.millisSince(speechEnd)
        val brainMs = reply.millisSince(transcript)
        val ttsMs = ttsStarted.millisSince(reply)
        val audioReportMs = stats.millisSince(ttsStarted)

        Log.i(
            TAG,
            "e2e latency (app-observed): endpointing+stt=${transcriptMs}ms brain=${brainMs}ms " +
                "tts_start=${ttsMs}ms tts_audio_report=${audioReportMs}ms " +
                "tts_audio=${audioBytes}B/${audioMs}ms transcript='$transcriptText' reply='$replyText'",
        )
        Log.i(TAG, "e2e trace (${CallTrace.snapshot().size} events): ${traceDump()}")

        assertLatency("endpointing and STT", transcriptMs, TRANSCRIPT_BUDGET_MS)
        assertLatency("brain reply", brainMs, REPLY_BUDGET_MS)
        assertLatency("TTS start", ttsMs, TTS_BUDGET_MS)
        assertLatency("TTS audio report", audioReportMs, TTS_BUDGET_MS)

        composeRule.onNodeWithTag("call_end").performClick()
        await("the call to end", END_TIMEOUT_MS) {
            !controller.uiState.value.state.isLive
        }
        assertTrue(
            "the call must finish in Ended, got ${controller.uiState.value.state}",
            controller.uiState.value.state is CallState.Ended,
        )
    }

    private fun finalTranscript(): CallTrace.Entry? =
        CallTrace.snapshot().firstOrNull {
            it.name == "user.transcript" && it.detail["final"] == "true"
        }

    /** One-line dump of the app's event stream with the gap between entries. */
    private fun traceDump(): String {
        val entries = CallTrace.snapshot()
        val builder = StringBuilder()
        entries.forEachIndexed { index, entry ->
            val gap = if (index == 0) 0L else entry.millisSince(entries[index - 1])
            if (index > 0) {
                builder.append(" | ")
            }
            builder.append(entry.name).append('+').append(gap).append("ms")
        }
        return builder.toString()
    }

    private fun assertLatency(label: String, observedMs: Long, budgetMs: Long) {
        assertTrue(
            "$label took ${observedMs}ms at the app, over the ${budgetMs}ms budget",
            observedMs in 0..budgetMs,
        )
    }

    private fun deviceToken(): String =
        InstrumentationRegistry.getArguments().getString("deviceToken")?.trim().orEmpty()

    /**
     * True only for the container relay: `/ready` answers 200 with
     * `{"status":"ready"}`. The stub relay does not serve `/ready`, so a fixture
     * left on the forwarded port cannot satisfy this check by accident.
     */
    private fun containerRelayReady(): Boolean {
        val body = try {
            get(RELAY_URL + READY_PATH) ?: return false
        } catch (_: Exception) {
            return false
        }
        return try {
            JSONObject(body).optString("status") == "ready"
        } catch (_: Exception) {
            false
        }
    }

    private fun get(url: String): String? {
        val connection = URL(url).openConnection() as HttpURLConnection
        return try {
            connection.requestMethod = "GET"
            connection.connectTimeout = PROBE_TIMEOUT_MS
            connection.readTimeout = PROBE_TIMEOUT_MS
            if (connection.responseCode != 200) {
                return null
            }
            connection.inputStream.bufferedReader().use { it.readText() }
        } finally {
            connection.disconnect()
        }
    }

    private fun launchWith(settings: RelaySettings) {
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

    private fun await(label: String, timeoutMs: Long, condition: () -> Boolean) {
        val deadline = SystemClock.uptimeMillis() + timeoutMs
        while (SystemClock.uptimeMillis() < deadline) {
            if (condition()) {
                return
            }
            SystemClock.sleep(POLL_INTERVAL_MS)
        }
        fail("timed out waiting for $label; trace: ${CallTrace.snapshot().map { it.name }}")
    }

    private companion object {
        const val TAG = "ContainerCallE2E"

        /** Loopback only: the container relay is reached through `adb reverse`. */
        const val RELAY_URL = "http://127.0.0.1:8790"
        const val READY_PATH = "/ready"

        /** Synthetic Kokoro speech: "What time is it?" with a silence tail. */
        const val FIXTURE_ASSET = "e2e/what_time_is_it.wav"
        const val EXPECTED_WORD = "time"

        const val PROBE_TIMEOUT_MS = 5_000
        const val LOAD_TIMEOUT_MS = 15_000L
        const val READY_TIMEOUT_MS = 60_000L
        const val CALL_TIMEOUT_MS = 45_000L
        const val FEED_TIMEOUT_MS = 30_000L
        const val TRANSCRIPT_TIMEOUT_MS = 20_000L
        const val REPLY_TIMEOUT_MS = 20_000L
        const val TTS_TIMEOUT_MS = 30_000L
        const val END_TIMEOUT_MS = 30_000L
        const val POLL_INTERVAL_MS = 100L

        /** Generous ceilings; the real figures are logged and recorded in docs/TESTING.md. */
        const val TRANSCRIPT_BUDGET_MS = 15_000L
        const val REPLY_BUDGET_MS = 15_000L
        const val TTS_BUDGET_MS = 20_000L
    }
}
