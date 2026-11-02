package com.example.hermestalk.wakeword

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

@RunWith(AndroidJUnit4::class)
class PushToTalkTest {

    @Test
    fun pressReleaseRequestsASessionAndLeavesNoCaptureRunning() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val directory = File(context.cacheDir, "wakeword-push-to-talk-${System.nanoTime()}")
        var captureCreated = false
        val controller = WakeWordController(
            initialConfig = WakeWordConfig(),
            engineFactory = { throw WakeWordEngineException("engine unavailable") },
            captureFactory = { _, _ ->
                captureCreated = true
                NoopCapture()
            },
            permissionChecker = WakeWordPermissionChecker { true },
            falseTriggerLog = FalseTriggerLog(directory, 65_536L),
            usage = WakeWordUsage(),
        )

        try {
            assertFalse(controller.start())
            assertEquals(
                WakeWordState.Unavailable("engine unavailable"),
                controller.state.value,
            )

            controller.onPushToTalkPressed()
            assertEquals(PushToTalkState.Pressed, controller.pushToTalkState.value)
            val released = controller.onPushToTalkReleased()
            assertEquals(PushToTalkState.Released, controller.pushToTalkState.value)
            assertTrue(
                released.effects.contains(PushToTalkEffect.StartSessionRequested),
            )
            assertFalse(captureCreated)
            assertFalse(controller.audioRecordCreated())
            assertTrue(controller.audioRecordReleased())
            assertFalse(controller.captureThreadAlive())
        } finally {
            controller.release()
            directory.deleteRecursively()
        }
    }

    private class NoopCapture : WakeWordCapture {
        override var onKeyword: ((String) -> Unit)? = null
        override var onLevel: ((Float) -> Unit)? = null
        override var onFailure: ((String) -> Unit)? = null
        override val running: Boolean = false
        override val threadAlive: Boolean = false
        override val audioRecordCreated: Boolean = false
        override val audioRecordReleased: Boolean = true

        override fun start(): WakeWordCaptureStart = WakeWordCaptureStart.Started
        override fun stop() = Unit
    }
}
