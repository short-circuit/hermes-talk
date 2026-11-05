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
import com.example.hermestalk.call.CallController
import com.example.hermestalk.call.CallControllers
import com.example.hermestalk.call.PersistentAssistant
import com.example.hermestalk.call.PersistentAssistantController
import com.example.hermestalk.call.PersistentAssistantService
import com.example.hermestalk.call.SharedPreferencesPersistentAssistantSettingsStore
import com.example.hermestalk.call.isLive
import com.example.hermestalk.rtvi.RelaySettings
import com.example.hermestalk.wakeword.SharedPreferencesWakeWordSettingsStore
import com.example.hermestalk.wakeword.WakeWordCaptureStart
import com.example.hermestalk.wakeword.WakeWordConfig
import com.example.hermestalk.wakeword.WakeWordEngine
import com.example.hermestalk.wakeword.WakeWordListener
import com.example.hermestalk.wakeword.WakeWordSettings
import com.example.hermestalk.wakeword.readWavFixture
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.launch
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
 * source is replaced. Each fixture is played on its own, so a missed positive
 * and a false trigger on a negative fail separately.
 *
 * Two cases deliberately bypass the capture hook: `liveCallTakesTheMicrophone…`
 * starts a real call through `CallControllers` and therefore exercises
 * `CallController` → `ProcessAssistantMicCoordinator` → the runner, and
 * `realAudioRecordListenReleaseCycleWorks` drives `WakeWordListener` against a
 * real `AudioRecord`.
 *
 * The soak test defaults to 1800 seconds and can be shortened through
 * `-e wakeWordSoakSeconds <seconds>` when running through
 * `adb shell instrument`.
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
        // The production launcher opens MainActivity, which starts a relay call
        // and suspends the wake word after the first detection. Stub only the
        // Activity launch so each fixture can be played on its own; the service,
        // runner, capture restart and keyword engine remain production code.
        AssistantWakeWordHooks.launcher = object : AssistantLauncher {
            override fun launchWakeWord(): AssistantLaunchResult =
                AssistantLaunchResult(started = true)
        }

        // Play every fixture one at a time, with a fresh runner for each, so a
        // miss in one positive cannot be masked by a detection in another and a
        // false trigger in one negative cannot be masked by the count of the
        // other fixtures.
        val misses = mutableListOf<String>()
        for (name in POSITIVE_FIXTURES) {
            val source = startSingleFixture(name)
            try {
                await("a detection for positive fixture $name", FIXTURE_TIMEOUT_MS) {
                    AssistantWakeWord.state.value.detectionCount >= 1
                }
            } catch (miss: AssertionError) {
                misses += name
                Log.w(USAGE_TAG, "positive_fixture=$name detections=0: ${miss.message}")
            } finally {
                Log.i(
                    USAGE_TAG,
                    "positive_fixture=$name detections=${AssistantWakeWord.state.value.detectionCount} " +
                        "finished=${source.finished}",
                )
                stopService()
            }
        }

        val falseTriggers = mutableListOf<String>()
        for (name in NEGATIVE_FIXTURES) {
            val source = startSingleFixture(name)
            try {
                await("the $name playback", FIXTURE_TIMEOUT_MS) { source.finished }
                // Let the engine drain the trailing silence and the cooldown gate
                // settle before counting.
                SystemClock.sleep(FIXTURE_SETTLE_MS)
                val detections = AssistantWakeWord.state.value.detectionCount
                Log.i(USAGE_TAG, "negative_fixture=$name detections=$detections")
                if (detections != 0) {
                    falseTriggers += "$name ($detections)"
                }
            } finally {
                stopService()
            }
        }

        // Report both failure modes in one message so neither can mask the
        // other: a missed positive and a false trigger on a negative are
        // separate findings, each named with its fixture.
        val problems = buildList {
            if (misses.isNotEmpty()) {
                add("positive fixtures produced no detection: $misses")
            }
            if (falseTriggers.isNotEmpty()) {
                add("negative fixtures produced a detection: $falseTriggers")
            }
        }
        assertTrue(problems.joinToString("\n"), problems.isEmpty())
    }

    @Test
    fun liveCallTakesTheMicrophoneAndReleasesTheAudioRecord() {
        // A scripted microphone keeps the fixture deterministic; the runner,
        // service, CallController and ProcessAssistantMicCoordinator are all
        // production code. The real AudioRecord path has its own case below.
        installCapture(FixturePlayback(readWavFixture(testAssets, NEGATIVE_FIXTURE)))
        enableStores()
        AssistantWakeWord.reset()

        stopService()
        PersistentAssistantController.startFromForeground(context)
        await("wake-word listening", START_TIMEOUT_MS) {
            AssistantWakeWord.state.value.listening
        }
        assertFalse(
            "AudioRecord must be held before the call",
            AssistantWakeWord.audioRecordReleased(),
        )

        // Record every published ownership state. The handover is a transition:
        // when the relay is unreachable the call fails a moment later and the
        // runner resumes, so Call ownership has to be observed as it happens
        // rather than sampled after startCall() returns.
        val samples = CopyOnWriteArrayList<MicOwnershipSample>()
        val observerScope = CoroutineScope(SupervisorJob() + Dispatchers.Unconfined)
        val observer = observerScope.launch {
            AssistantWakeWord.state.collect { state ->
                samples += MicOwnershipSample(
                    owner = state.micOwner,
                    audioRecordReleased = AssistantWakeWord.audioRecordReleased(),
                    atMillis = SystemClock.uptimeMillis(),
                )
            }
        }

        lateinit var controller: CallController
        try {
            instrumentation.runOnMainSync {
                controller = CallControllers.get(context)
                controller.startCall(
                    RelaySettings(relayUrl = CALL_RELAY_URL, deviceToken = ""),
                )
            }

            // The CallController's start effect calls
            // ProcessAssistantMicCoordinator.suspendWakeWord() synchronously, so
            // the wake-word AudioRecord must already be released here.
            assertTrue(
                "the call did not release the wake-word AudioRecord",
                AssistantWakeWord.audioRecordReleased(),
            )
            assertFalse(
                "the capture thread survived the call start",
                AssistantWakeWord.captureThreadAlive(),
            )

            await("the microphone to be handed to the call", HANDOVER_TIMEOUT_MS) {
                samples.any { it.owner == AssistantMicOwner.Call }
            }
            val handover = samples.first { it.owner == AssistantMicOwner.Call }
            assertEquals(
                "the runner must report the call as the microphone owner",
                AssistantMicOwner.Call,
                handover.owner,
            )
            assertTrue(
                "micOwner was Call while the wake-word AudioRecord was still held",
                handover.audioRecordReleased,
            )
            if (controller.uiState.value.state.isLive) {
                assertFalse(
                    "wake-word capture resumed while the call was still live",
                    AssistantWakeWord.state.value.listening,
                )
            }
            Log.i(
                USAGE_TAG,
                "call_handover mic_owner=${handover.owner} " +
                    "audio_record_released=${handover.audioRecordReleased} " +
                    "observed=${samples.map { it.owner.name }}",
            )
        } finally {
            instrumentation.runOnMainSync {
                if (controller.uiState.value.state.isLive) {
                    controller.endCall()
                }
            }
            observer.cancel()
            observerScope.cancel()
        }

        // The call released the microphone: the runner must take it back and
        // report wake-word ownership again.
        await("wake-word capture after the call", RESUME_TIMEOUT_MS) {
            val state = AssistantWakeWord.state.value
            state.listening &&
                state.micOwner == AssistantMicOwner.WakeWord &&
                !AssistantWakeWord.audioRecordReleased()
        }
    }

    @Test
    fun realAudioRecordListenReleaseCycleWorks() {
        // No capture hook is installed: the production WakeWordListener opens a
        // real AudioRecord and runs its own capture thread. The emulator
        // microphone is silent, so this proves the listen/release cycle and that
        // the read loop delivers a chunk, not a detection.
        AssistantWakeWordHooks.reset()
        val config = WakeWordConfig()
        val engine = WakeWordEngine.create(context, config)
        val failures = CopyOnWriteArrayList<String>()
        val chunks = AtomicInteger(0)
        val listener = WakeWordListener(
            engine = engine,
            permissionChecker = WakeWordListener.permissionChecker(context),
            config = config,
        )
        listener.onFailure = { message -> failures += message }
        listener.onLevel = { chunks.incrementAndGet() }

        try {
            assertEquals(
                "the real AudioRecord could not be started",
                WakeWordCaptureStart.Started,
                listener.start(),
            )
            assertTrue("AudioRecord was not created", listener.audioRecordCreated)
            assertFalse(
                "AudioRecord was released right after start",
                listener.audioRecordReleased,
            )
            assertTrue("the capture thread is not running", listener.threadAlive)

            await("a chunk from the real AudioRecord", RECORD_READ_TIMEOUT_MS) {
                chunks.get() > 0
            }
            assertTrue("the capture thread died", listener.threadAlive)
            assertTrue("the listener reported a failure: $failures", failures.isEmpty())
        } finally {
            listener.stop()
            engine.release()
        }

        assertFalse("the capture loop survived stop()", listener.running)
        assertTrue("AudioRecord was not released", listener.audioRecordReleased)
        assertFalse("the capture thread survived stop()", listener.threadAlive)
        Log.i(
            USAGE_TAG,
            "real_audio_record chunks=${chunks.get()} failures=${failures.size}",
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

    /**
     * Start a fresh runner that plays exactly [fixtureName] once, then silence.
     * A new service per fixture gives each one a fresh keyword engine and a
     * fresh cooldown gate, so detections cannot leak between fixtures.
     */
    private fun startSingleFixture(fixtureName: String): FixtureSequencePlayback {
        val source = FixtureSequencePlayback(
            listOf(readWavFixture(testAssets, "wakeword/$fixtureName")),
            gapMillis = 0L,
        )
        installCapture(source)
        enableStores()
        AssistantWakeWord.reset()

        stopService()
        PersistentAssistantController.startFromForeground(context)
        await("wake-word listening for $fixtureName", START_TIMEOUT_MS) {
            AssistantWakeWord.state.value.listening
        }
        return source
    }

    /** One published microphone-ownership observation. */
    private data class MicOwnershipSample(
        val owner: AssistantMicOwner,
        val audioRecordReleased: Boolean,
        val atMillis: Long,
    )

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
        // A single fixture is 2-3 s of audio plus service start/stop, so one
        // fixture gets a generous but bounded window.
        const val FIXTURE_TIMEOUT_MS = 45_000L
        const val FIXTURE_SETTLE_MS = 3_000L
        const val HANDOVER_TIMEOUT_MS = 20_000L
        const val RESUME_TIMEOUT_MS = 30_000L
        const val RECORD_READ_TIMEOUT_MS = 20_000L
        const val CALL_RELAY_URL = "http://127.0.0.1:8790"
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
