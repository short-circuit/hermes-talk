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
class WakeWordPermissionTest {

    @Test
    fun deniedPermissionReachesPermissionDeniedWithoutAudioRecord() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val directory = File(context.cacheDir, "wakeword-permission-${System.nanoTime()}")
        val controller = WakeWordController(
            initialConfig = WakeWordConfig(),
            engineFactory = { error("the engine must not be created without permission") },
            captureFactory = { _, _ -> error("capture must not be created without permission") },
            permissionChecker = WakeWordPermissionChecker { false },
            falseTriggerLog = FalseTriggerLog(directory, 65_536L),
            usage = WakeWordUsage(),
        )

        try {
            assertFalse(controller.start())
            assertEquals(WakeWordState.PermissionDenied, controller.state.value)
            assertFalse(controller.audioRecordCreated())
            assertTrue(controller.audioRecordReleased())
            assertFalse(controller.captureThreadAlive())
        } finally {
            controller.release()
            directory.deleteRecursively()
        }

        val listener = WakeWordListener(
            engine = NoopEngine(),
            permissionChecker = WakeWordPermissionChecker { false },
            config = WakeWordConfig(),
        )
        assertEquals(WakeWordCaptureStart.PermissionDenied, listener.start())
        assertFalse(listener.audioRecordCreated)
    }

    private class NoopEngine : WakeWordKeywordEngine {
        override fun newStream() = Unit
        override fun accept(samples: FloatArray, sampleRate: Int) = Unit
        override fun poll(): String? = null
        override fun release() = Unit
    }
}
