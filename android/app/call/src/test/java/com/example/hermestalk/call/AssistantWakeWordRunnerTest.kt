package com.example.hermestalk.call

import com.example.hermestalk.wakeword.FalseTriggerLog
import com.example.hermestalk.wakeword.WakeWordCapture
import com.example.hermestalk.wakeword.WakeWordCaptureStart
import com.example.hermestalk.wakeword.WakeWordConfig
import com.example.hermestalk.wakeword.WakeWordController
import com.example.hermestalk.wakeword.WakeWordKeywordEngine
import com.example.hermestalk.wakeword.WakeWordPermissionChecker
import com.example.hermestalk.wakeword.WakeWordUsage
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.runTest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File

class AssistantWakeWordRunnerTest {

    private lateinit var directory: File
    private lateinit var log: FalseTriggerLog

    @Before
    fun setUp() {
        directory = File(
            System.getProperty("java.io.tmpdir"),
            "assistant-wakeword-runner-${System.nanoTime()}",
        )
        directory.mkdirs()
        log = FalseTriggerLog(directory, 65_536L)
        AssistantWakeWord.reset()
        AssistantWakeWord.runner = null
    }

    @After
    fun tearDown() {
        AssistantWakeWord.reset()
        AssistantWakeWord.runner = null
        directory.deleteRecursively()
    }

    @Test
    fun detectionRunsTheOrderedStepsAndResumesAfterTheDelay() = runTest {
        val harness = Harness(this, log)
        AssistantWakeWord.runner = harness.runner
        harness.runner.setEnabled(true)
        testScheduler.runCurrent()

        assertEquals(1, harness.capture.startCount)
        assertTrue(AssistantWakeWord.state.value.listening)

        harness.capture.fireKeyword()
        testScheduler.runCurrent()

        assertEquals(1, harness.launcher.calls)
        assertEquals(1, harness.audioRestore.calls)
        val stopIndex = harness.events.indexOf("capture.stop")
        val audioIndex = harness.events.indexOf("audioRestore")
        val launchIndex = harness.events.indexOf("launcher")
        assertTrue("capture must stop", stopIndex >= 0)
        assertTrue("audio restore must run before the launch", stopIndex < audioIndex)
        assertTrue("the launch must be last", audioIndex < launchIndex)
        assertTrue(
            "the listening release must precede the audio restore",
            harness.events.indexOf("listening:false") in stopIndex..audioIndex,
        )

        val state = AssistantWakeWord.state.value
        assertEquals(1, state.detectionCount)
        assertTrue(state.falseTriggerCount >= 1)
        assertTrue(state.lastLaunchStarted)
        assertTrue(state.lastLaunchWithinBudget)
        assertTrue(state.audioRestored)
        assertEquals(AssistantMicOwner.None, state.micOwner)
        assertFalse(state.listening)

        assertEquals(1, harness.capture.startCount)
        testScheduler.advanceTimeBy(499)
        testScheduler.runCurrent()
        assertEquals(1, harness.capture.startCount)

        testScheduler.advanceTimeBy(1)
        testScheduler.runCurrent()
        assertEquals(2, harness.capture.startCount)
        assertTrue(AssistantWakeWord.state.value.listening)

        harness.close()
    }

    @Test
    fun lastLaunchWithinBudgetFollowsTheMeasuredLatency() = runTest {
        val harness = Harness(this, log, launcherAdvanceMillis = 1_500L)
        AssistantWakeWord.runner = harness.runner
        harness.runner.setEnabled(true)
        testScheduler.runCurrent()

        harness.capture.fireKeyword()
        testScheduler.runCurrent()

        val state = AssistantWakeWord.state.value
        assertTrue(state.lastLaunchLatencyMillis > 1_000L)
        assertFalse(state.lastLaunchWithinBudget)

        harness.close()
    }

    @Test
    fun suspendReleasesCaptureAndALiveCallPreventsResume() = runTest {
        val harness = Harness(this, log)
        AssistantWakeWord.runner = harness.runner
        harness.runner.setEnabled(true)
        testScheduler.runCurrent()
        assertTrue(AssistantWakeWord.state.value.listening)

        assertTrue(harness.runner.suspendForCall())
        assertTrue(harness.runner.audioRecordReleased())
        assertFalse(AssistantWakeWord.state.value.listening)
        assertEquals(1, harness.capture.startCount)

        harness.runner.onCallLiveChanged(true)
        testScheduler.advanceTimeBy(1_000)
        testScheduler.runCurrent()
        assertEquals(1, harness.capture.startCount)

        harness.runner.resumeAfterCall()
        testScheduler.runCurrent()
        assertEquals(1, harness.capture.startCount)

        harness.runner.onCallLiveChanged(false)
        testScheduler.runCurrent()
        assertEquals(2, harness.capture.startCount)
        assertTrue(AssistantWakeWord.state.value.listening)

        harness.close()
    }

    @Test
    fun disablingStopsCapture() = runTest {
        val harness = Harness(this, log)
        AssistantWakeWord.runner = harness.runner
        harness.runner.setEnabled(true)
        testScheduler.runCurrent()
        assertTrue(AssistantWakeWord.state.value.listening)

        harness.runner.setEnabled(false)
        assertFalse(AssistantWakeWord.state.value.listening)
        assertTrue(harness.capture.releasedFlag)
        assertEquals(1, harness.capture.startCount)

        harness.close()
    }

    @Test
    fun releaseIsIdempotent() = runTest {
        val harness = Harness(this, log)
        AssistantWakeWord.runner = harness.runner
        harness.runner.setEnabled(true)
        testScheduler.runCurrent()

        harness.runner.release()
        harness.runner.release()

        assertEquals(1, harness.engines.sumOf { it.releaseCalls })
        assertNull(AssistantWakeWord.runner)

        harness.close()
    }

    private class Harness(
        testScope: TestScope,
        log: FalseTriggerLog,
        launcherAdvanceMillis: Long = 0L,
        var enabled: Boolean = true,
        var foregroundType: AssistantForegroundType = AssistantForegroundType.Microphone,
        var hasRecordAudio: Boolean = true,
    ) {
        val events = mutableListOf<String>()
        val clock = FakeClock()
        val capture = FakeCapture(events)
        val engines = mutableListOf<FakeEngine>()
        val controllerScope =
            CoroutineScope(SupervisorJob() + StandardTestDispatcher(testScope.testScheduler))
        lateinit var runner: AssistantWakeWordRunner
        val controller = WakeWordController(
            initialConfig = WakeWordConfig(),
            engineFactory = {
                FakeEngine().also { engines += it }
            },
            captureFactory = { _, _ -> capture },
            permissionChecker = WakeWordPermissionChecker { true },
            falseTriggerLog = log,
            usage = WakeWordUsage(clock = { clock.now }, cpuTicksReader = { 0L }),
            onWakeWord = { runner.handleDetection() },
            scope = controllerScope,
        )
        val runnerScope =
            CoroutineScope(SupervisorJob() + StandardTestDispatcher(testScope.testScheduler))
        val launcher = FakeLauncher(clock, launcherAdvanceMillis, events)
        val audioRestore = FakeAudioRestore(events)

        init {
            runner = AssistantWakeWordRunner(
                controllerFactory = { controller },
                launcher = launcher,
                audioRestore = audioRestore,
                clock = { clock.read() },
                onListeningChanged = { listening -> events += "listening:$listening" },
                restartDelayMillis = 500L,
                scope = runnerScope,
                foregroundTypeProvider = { foregroundType },
                callLiveProvider = { runner.callLive },
                hasRecordAudioProvider = { hasRecordAudio },
                enabledProvider = { enabled },
                falseTriggerLog = log,
            )
        }

        fun close() {
            runner.release()
            controllerScope.cancel()
            runnerScope.cancel()
        }
    }

    private class FakeClock(var now: Long = 1_000L) {
        fun read(): Long {
            val value = now
            now += 1
            return value
        }

        fun advance(millis: Long) {
            now += millis
        }
    }

    private class FakeEngine : WakeWordKeywordEngine {
        var releaseCalls = 0

        override fun newStream() = Unit

        override fun accept(samples: FloatArray, sampleRate: Int) = Unit

        override fun poll(): String? = null

        override fun release() {
            releaseCalls += 1
        }
    }

    private class FakeCapture(
        private val events: MutableList<String>,
    ) : WakeWordCapture {
        override var onKeyword: ((String) -> Unit)? = null
        override var onLevel: ((Float) -> Unit)? = null
        override var onFailure: ((String) -> Unit)? = null

        var startCount = 0
        var runningFlag = false
        var threadAliveFlag = false
        var createdFlag = false
        var releasedFlag = true

        override val running: Boolean
            get() = runningFlag

        override val threadAlive: Boolean
            get() = threadAliveFlag

        override val audioRecordCreated: Boolean
            get() = createdFlag

        override val audioRecordReleased: Boolean
            get() = releasedFlag

        override fun start(): WakeWordCaptureStart {
            startCount += 1
            runningFlag = true
            threadAliveFlag = true
            createdFlag = true
            releasedFlag = false
            events += "capture.start"
            return WakeWordCaptureStart.Started
        }

        override fun stop() {
            runningFlag = false
            threadAliveFlag = false
            releasedFlag = true
            events += "capture.stop"
        }

        fun fireKeyword() {
            onKeyword?.invoke("HEY HERMES")
        }
    }

    private class FakeLauncher(
        private val clock: FakeClock,
        private val advanceMillis: Long,
        private val events: MutableList<String>,
    ) : AssistantLauncher {
        var calls = 0

        override fun launchWakeWord(): AssistantLaunchResult {
            calls += 1
            events += "launcher"
            clock.advance(advanceMillis)
            return AssistantLaunchResult(
                started = true,
                latencyMillis = advanceMillis,
            )
        }
    }

    private class FakeAudioRestore(
        private val events: MutableList<String>,
    ) : AssistantAudioRestore {
        var calls = 0

        override fun restoreAfterDetection(): Boolean {
            calls += 1
            events += "audioRestore"
            return true
        }
    }
}
