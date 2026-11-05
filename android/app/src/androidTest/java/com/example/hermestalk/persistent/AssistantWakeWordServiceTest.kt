package com.example.hermestalk.persistent

import android.Manifest
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.SystemClock
import android.util.Log
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.rule.GrantPermissionRule
import androidx.test.uiautomator.By
import androidx.test.uiautomator.UiDevice
import com.example.hermestalk.call.AssistantLaunchResult
import com.example.hermestalk.call.AssistantLauncher
import com.example.hermestalk.call.AssistantMicOwner
import com.example.hermestalk.call.AssistantWakeWord
import com.example.hermestalk.call.AssistantWakeWordController
import com.example.hermestalk.call.AssistantWakeWordHooks
import com.example.hermestalk.call.AssistantWakeWordPolicy
import com.example.hermestalk.call.CallControllers
import com.example.hermestalk.call.PersistentAssistant
import com.example.hermestalk.call.PersistentAssistantController
import com.example.hermestalk.call.PersistentAssistantService
import com.example.hermestalk.call.SharedPreferencesPersistentAssistantSettingsStore
import com.example.hermestalk.wakeword.SharedPreferencesWakeWordSettingsStore
import com.example.hermestalk.wakeword.WakeWordSettings
import com.example.hermestalk.wakeword.readWavFixture
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Instrumentation proof for the always-on wake word inside the persistent
 * assistant service.
 *
 * A scripted microphone replays the checked-in 16 kHz fixtures through the real
 * on-device keyword engine. The runner, service, audio cleanup, ACTION_ASSIST
 * launch and microphone arbitration are all production code; only the capture
 * source is replaced.
 *
 * The soak test defaults to 1800 seconds and can be shortened through
 * `-e wakeWordSoakSeconds <seconds>` when running through
 * `adb shell am instrument`.
 */
@RunWith(AndroidJUnit4::class)
class AssistantWakeWordServiceTest {

    @get:Rule
    val permissions: GrantPermissionRule =
        GrantPermissionRule.grant(Manifest.permission.RECORD_AUDIO)

    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context: Context = instrumentation.targetContext.applicationContext
    private val testAssets = instrumentation.context.assets

    @Before
    fun setUp() {
        instrumentation.runOnMainSync {
            runCatching { CallControllers.get(context).endCall() }
        }
        stopService()
        SharedPreferencesPersistentAssistantSettingsStore(context).setEnabled(false)
        SharedPreferencesWakeWordSettingsStore(context).save(
            WakeWordSettings(enabled = false),
        )
        AssistantWakeWordHooks.reset()
        AssistantWakeWord.reset()
        grantNotificationPermissionIfNeeded()
    }

    @After
    fun tearDown() {
        stopService()
        AssistantWakeWordHooks.reset()
        AssistantWakeWord.reset()
        SharedPreferencesPersistentAssistantSettingsStore(context).setEnabled(false)
        SharedPreferencesWakeWordSettingsStore(context).save(
            WakeWordSettings(enabled = false),
        )
        runCatching {
            UiDevice.getInstance(instrumentation).pressHome()
        }
    }

    @Test
    fun wakeWordDetectionLaunchesTheAssistantWithinBudget() {
        val playback = FixturePlayback(
            readWavFixture(testAssets, POSITIVE_FIXTURE),
        )
        installCapture(playback)
        enableStores()
        AssistantWakeWord.reset()

        stopService()
        PersistentAssistantController.startFromForeground(context)

        await("wake-word listening", START_TIMEOUT_MS) {
            AssistantWakeWord.state.value.listening
        }
        await("wake-word detection and launch", DETECTION_TIMEOUT_MS) {
            val state = AssistantWakeWord.state.value
            state.detectionCount >= 1 && state.lastLaunchStarted
        }

        val state = AssistantWakeWord.state.value
        assertTrue("the assistant did not launch", state.lastLaunchStarted)
        assertTrue(
            "launch latency ${state.lastLaunchLatencyMillis} ms exceeds the " +
                "${AssistantWakeWordPolicy.DETECTION_TO_CALL_BUDGET_MILLIS} ms budget",
            state.lastLaunchWithinBudget,
        )
        assertTrue(
            "the audio path was not restored after detection",
            state.audioRestored,
        )

        val device = UiDevice.getInstance(instrumentation)
        awaitCallScreen(device, CALL_SCREEN_TIMEOUT_MS)
        Log.i(
            USAGE_TAG,
            "detection_launch_latency_ms=${state.lastLaunchLatencyMillis} " +
                "within_budget=${state.lastLaunchWithinBudget} " +
                "detection_count=${state.detectionCount} " +
                "false_trigger_count=${state.falseTriggerCount}",
        )
    }

    @Test
    fun liveCallSuspendsWakeWordAndResumeRestoresCapture() {
        val playback = FixturePlayback(
            readWavFixture(testAssets, NEGATIVE_FIXTURE),
        )
        installCapture(playback)
        enableStores()
        AssistantWakeWord.reset()

        stopService()
        PersistentAssistantController.startFromForeground(context)
        await("wake-word listening", START_TIMEOUT_MS) {
            AssistantWakeWord.state.value.listening
        }
        assertFalse(
            "AudioRecord must be held while listening",
            AssistantWakeWord.audioRecordReleased(),
        )
        assertTrue(
            "the capture thread must be alive while listening",
            AssistantWakeWord.captureThreadAlive(),
        )

        assertTrue(
            "a live call must suspend wake-word capture",
            AssistantWakeWord.suspendForCall(),
        )
        assertFalse(AssistantWakeWord.state.value.listening)
        assertTrue(
            "AudioRecord was not released for the call",
            AssistantWakeWord.audioRecordReleased(),
        )
        assertFalse(
            "the capture thread survived the call",
            AssistantWakeWord.captureThreadAlive(),
        )

        SystemClock.sleep(SUSPEND_SETTLE_MS)
        assertFalse(
            "wake-word capture restarted while the call was live",
            AssistantWakeWord.state.value.listening,
        )

        AssistantWakeWord.resumeAfterCall()
        await("wake-word listening after the call", START_TIMEOUT_MS) {
            AssistantWakeWord.state.value.listening
        }
        assertEquals(
            AssistantMicOwner.WakeWord,
            AssistantWakeWord.state.value.micOwner,
        )
        assertFalse(
            "AudioRecord must be reacquired after the call",
            AssistantWakeWord.audioRecordReleased(),
        )

        stopService()
        await("audio released after service stop", STOP_TIMEOUT_MS) {
            AssistantWakeWord.audioRecordReleased()
        }
        assertFalse(
            "the capture thread survived service teardown",
            AssistantWakeWord.captureThreadAlive(),
        )
    }

    @Test
    fun disablingWakeWordStopsCaptureAndReleasesAudio() {
        val playback = FixturePlayback(
            readWavFixture(testAssets, NEGATIVE_FIXTURE),
        )
        installCapture(playback)
        enableStores()
        AssistantWakeWord.reset()

        stopService()
        PersistentAssistantController.startFromForeground(context)
        await("wake-word listening", START_TIMEOUT_MS) {
            AssistantWakeWord.state.value.listening
        }

        AssistantWakeWordController.setEnabled(context, false)
        await("wake-word disabled", STOP_TIMEOUT_MS) {
            !AssistantWakeWord.state.value.enabled &&
                !AssistantWakeWord.state.value.listening
        }
        assertTrue(
            "AudioRecord was not released after disabling",
            AssistantWakeWord.audioRecordReleased(),
        )
        assertFalse(
            "the capture thread survived disabling",
            AssistantWakeWord.captureThreadAlive(),
        )

        AssistantWakeWordController.setEnabled(context, true)
        await("wake-word re-enabled", START_TIMEOUT_MS) {
            AssistantWakeWord.state.value.enabled &&
                AssistantWakeWord.state.value.listening
        }
    }

    @Test
    fun fixturesAreDetectedWithZeroMissesAndNoFalseTriggers() {
        val names = POSITIVE_FIXTURES + NEGATIVE_FIXTURES
        val source = FixtureSequencePlayback(
            names.map { readWavFixture(testAssets, "wakeword/$it") },
        )
        // The production launcher opens MainActivity, which starts a relay call
        // and suspends the wake word after the first detection. Stub only the
        // Activity launch so the whole sequence can run; the service, runner,
        // capture restart and keyword engine remain production code.
        AssistantWakeWordHooks.launcher = object : AssistantLauncher {
            override fun launchWakeWord(): AssistantLaunchResult =
                AssistantLaunchResult(started = true)
        }
        installCapture(source)
        enableStores()
        AssistantWakeWord.reset()

        stopService()
        PersistentAssistantController.startFromForeground(context)
        await("wake-word listening", START_TIMEOUT_MS) {
            AssistantWakeWord.state.value.listening
        }
        await("all ${POSITIVE_FIXTURES.size} positives detected", FIXTURE_SET_TIMEOUT_MS) {
            AssistantWakeWord.state.value.detectionCount >= POSITIVE_FIXTURES.size
        }
        await("fixture sequence finished", FIXTURE_SET_TIMEOUT_MS) { source.finished }
        // let the cooldown gate close after the last accepted detection, then prove
        // the negatives added nothing.
        SystemClock.sleep(COOLDOWN_SETTLE_MS)

        val state = AssistantWakeWord.state.value
        Log.i(
            USAGE_TAG,
            "fixture_set positives=${POSITIVE_FIXTURES.size} negatives=${NEGATIVE_FIXTURES.size} " +
                "detections=${state.detectionCount} false_trigger_count=${state.falseTriggerCount}",
        )
        assertEquals(
            "expected one detection per positive fixture and none from the negatives",
            POSITIVE_FIXTURES.size,
            state.detectionCount,
        )
        assertTrue(
            "the false-trigger log did not record the accepted detections",
            state.falseTriggerCount >= POSITIVE_FIXTURES.size,
        )
    }

    @LargeTest
    @Test
    fun continuousListeningForTheConfiguredWindow() {
        val playback = FixturePlayback(
            readWavFixture(testAssets, NEGATIVE_FIXTURE),
        )
        installCapture(playback)
        enableStores()
        AssistantWakeWord.reset()

        val soakSeconds = InstrumentationRegistry.getArguments()
            .getString("wakeWordSoakSeconds")
            ?.toLongOrNull()
            ?.coerceAtLeast(5L)
            ?: DEFAULT_SOAK_SECONDS
        val soakMillis = soakSeconds * 1000L

        stopService()
        PersistentAssistantController.startFromForeground(context)
        await("wake-word listening", START_TIMEOUT_MS) {
            AssistantWakeWord.state.value.listening
        }

        val device = UiDevice.getInstance(instrumentation)
        val pidBefore = pidOf(device)
        assertTrue("could not read the app pid", pidBefore.isNotBlank())

        try {
            device.sleep()
            SystemClock.sleep(soakMillis)

            val pidAfter = pidOf(device)
            assertEquals(
                "the app process was replaced during the soak",
                pidBefore,
                pidAfter,
            )
            val state = AssistantWakeWord.state.value
            assertTrue(
                "the persistent assistant stopped during the soak",
                PersistentAssistant.state.value.running,
            )
            assertTrue(
                "wake-word capture stopped during the soak",
                state.listening,
            )
            assertFalse(
                "AudioRecord was released during the soak",
                AssistantWakeWord.audioRecordReleased(),
            )
            assertTrue(
                "the capture thread died during the soak",
                AssistantWakeWord.captureThreadAlive(),
            )

            val usage = AssistantWakeWord.usageSnapshot()
            assertNotNull("no wake-word usage snapshot", usage)
            Log.i(
                USAGE_TAG,
                "listening_wall_ms=${usage!!.listeningWallMillis} " +
                    "cpu_ms=${usage.cpuMillis} " +
                    "detection_count=${usage.detectionCount}",
            )
            assertTrue(
                "listening window was ${usage.listeningWallMillis} ms, " +
                    "expected at least ${soakMillis - LISTENING_TOLERANCE_MS} ms",
                usage.listeningWallMillis >= soakMillis - LISTENING_TOLERANCE_MS,
            )
            assertTrue("CPU millis must be non-zero", usage.cpuMillis > 0L)
        } finally {
            device.wakeUp()
            stopService()
        }

        await("audio released after service stop", STOP_TIMEOUT_MS) {
            AssistantWakeWord.audioRecordReleased()
        }
        assertFalse(
            "the capture thread survived service teardown",
            AssistantWakeWord.captureThreadAlive(),
        )
    }

    private fun installCapture(source: WakeWordSampleSource) {
        AssistantWakeWordHooks.captureFactory = { engine, config ->
            FixtureWakeWordCapture(engine, config, source)
        }
    }

    private fun enableStores() {
        SharedPreferencesWakeWordSettingsStore(context).save(
            WakeWordSettings(enabled = true),
        )
        SharedPreferencesPersistentAssistantSettingsStore(context).setEnabled(true)
    }

    private fun grantNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            try {
                instrumentation.uiAutomation.grantRuntimePermission(
                    context.packageName,
                    Manifest.permission.POST_NOTIFICATIONS,
                )
            } catch (_: Exception) {
                // The foreground notification is still posted without it.
            }
        }
    }

    private fun stopService() {
        context.stopService(Intent(context, PersistentAssistantService::class.java))
        await("service stopped", STOP_TIMEOUT_MS) {
            !PersistentAssistant.state.value.running
        }
    }

    private fun pidOf(device: UiDevice): String =
        device.executeShellCommand("pidof ${context.packageName}").trim()

    /**
     * Wait for the Compose call-screen root. Depending on the platform, the
     * `testTagsAsResourceId` tag is exposed either as the bare `call_screen`
     * resource id or as `<package>:id/call_screen`; accept both forms.
     */
    private fun awaitCallScreen(device: UiDevice, timeoutMs: Long) {
        val byBareTag = By.res("call_screen")
        val byPackageTag = By.res(context.packageName, "call_screen")
        val deadline = SystemClock.uptimeMillis() + timeoutMs
        while (SystemClock.uptimeMillis() < deadline) {
            if (
                device.findObject(byBareTag) != null ||
                device.findObject(byPackageTag) != null
            ) {
                return
            }
            SystemClock.sleep(POLL_INTERVAL_MS)
        }
        fail(
            "timed out waiting for call_screen; dump:\n" +
                device.executeShellCommand("uiautomator dump /dev/tty"),
        )
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
        const val POSITIVE_FIXTURE = "wakeword/hey_hermes_af_heart.wav"
        const val NEGATIVE_FIXTURE = "wakeword/negative_af_heart.wav"
        val POSITIVE_FIXTURES = listOf(
            "hey_hermes_af_heart.wav", "hey_hermes_af_bella.wav", "hey_hermes_am_michael.wav",
            "hey_hermes_am_adam.wav", "hey_hermes_bf_emma.wav", "hey_hermes_bm_george.wav",
            "hey_hermes_af_jessica.wav", "hey_hermes_bf_alice.wav", "hey_hermes_af_nicole.wav",
            "hey_hermes_bm_lewis.wav",
        )
        val NEGATIVE_FIXTURES = listOf(
            "negative_af_heart.wav", "negative_am_michael.wav", "negative_bf_emma.wav",
        )
        const val FIXTURE_SET_TIMEOUT_MS = 300_000L
        const val COOLDOWN_SETTLE_MS = 4_000L
        const val USAGE_TAG = "WakeWordUsage"
        const val DEFAULT_SOAK_SECONDS = 1_800L
        const val START_TIMEOUT_MS = 30_000L
        const val DETECTION_TIMEOUT_MS = 20_000L
        const val CALL_SCREEN_TIMEOUT_MS = 20_000L
        const val STOP_TIMEOUT_MS = 10_000L
        const val SUSPEND_SETTLE_MS = 1_000L
        const val LISTENING_TOLERANCE_MS = 5_000L
        const val POLL_INTERVAL_MS = 100L
    }
}
