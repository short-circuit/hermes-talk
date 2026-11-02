package com.example.hermestalk.wakeword

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File

class WakeWordControllerTest {

    private lateinit var directory: File

    @Before
    fun setUp() {
        directory = File(
            System.getProperty("java.io.tmpdir"),
            "wakeword-controller-${System.nanoTime()}",
        )
        directory.mkdirs()
    }

    @After
    fun tearDown() {
        directory.deleteRecursively()
    }

    private fun controller(
        engineFactory: (WakeWordConfig) -> WakeWordKeywordEngine,
        captureFactory: (WakeWordKeywordEngine, WakeWordConfig) -> WakeWordCapture = { _, _ ->
            error("capture should not be created")
        },
        permissionChecker: WakeWordPermissionChecker = WakeWordPermissionChecker { true },
    ): WakeWordController = WakeWordController(
        initialConfig = WakeWordConfig(),
        engineFactory = engineFactory,
        captureFactory = captureFactory,
        permissionChecker = permissionChecker,
        falseTriggerLog = FalseTriggerLog(directory, 65_536L),
        usage = WakeWordUsage(clock = { 1_000L }, cpuTicksReader = { 0L }),
        scope = CoroutineScope(UnconfinedTestDispatcher()),
    )

    @Test
    fun deniedPermissionReachesPermissionDeniedWithoutCreatingEngineOrCapture() {
        var engineCreated = false
        var captureCreated = false
        val controller = controller(
            engineFactory = {
                engineCreated = true
                error("engine should not be created")
            },
            captureFactory = { _, _ ->
                captureCreated = true
                error("capture should not be created")
            },
            permissionChecker = WakeWordPermissionChecker { false },
        )

        assertFalse(controller.start())
        assertEquals(WakeWordState.PermissionDenied, controller.state.value)
        assertFalse(engineCreated)
        assertFalse(captureCreated)
        assertFalse(controller.audioRecordCreated())
        assertTrue(controller.audioRecordReleased())
        assertFalse(controller.captureThreadAlive())
        controller.release()
    }

    @Test
    fun missingEngineMarksUnavailableAndPushToTalkStillWorks() {
        val controller = controller(
            engineFactory = { throw WakeWordEngineException("native library missing") },
        )

        assertFalse(controller.start())
        assertEquals(WakeWordState.Unavailable("native library missing"), controller.state.value)

        val pressed = controller.onPushToTalkPressed()
        assertEquals(PushToTalkState.Pressed, pressed.state)
        val released = controller.onPushToTalkReleased()
        assertEquals(PushToTalkState.Released, released.state)
        assertEquals(
            listOf(PushToTalkEffect.StartSessionRequested),
            released.effects,
        )
        assertFalse(controller.audioRecordCreated())
        assertTrue(controller.audioRecordReleased())
        assertFalse(controller.captureThreadAlive())
        controller.release()
    }

    @Test
    fun stopFromUnavailableReturnsToOff() {
        val controller = controller(
            engineFactory = { throw WakeWordEngineException("no model") },
        )
        controller.start()
        assertEquals(WakeWordState.Unavailable("no model"), controller.state.value)
        controller.stop()
        assertEquals(WakeWordState.Off, controller.state.value)
        controller.release()
    }

    @Test
    fun updateConfigWhileOffDoesNotCreateResources() {
        var engineCalls = 0
        val controller = controller(
            engineFactory = {
                engineCalls += 1
                FakeEngine()
            },
        )
        controller.updateConfig(WakeWordConfig(sensitivity = WakeWordSensitivity.Strict))
        assertEquals(0, engineCalls)
        assertEquals(WakeWordState.Off, controller.state.value)
        controller.release()
    }

    private class FakeEngine : WakeWordKeywordEngine {
        override fun newStream() = Unit
        override fun accept(samples: FloatArray, sampleRate: Int) = Unit
        override fun poll(): String? = null
        override fun release() = Unit
    }
}
