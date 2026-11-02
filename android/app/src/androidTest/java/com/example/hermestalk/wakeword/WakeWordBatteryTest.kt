package com.example.hermestalk.wakeword

import android.Manifest
import android.util.Log
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.rule.GrantPermissionRule
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
@LargeTest
class WakeWordBatteryTest {

    @get:Rule
    val permissionRule: GrantPermissionRule =
        GrantPermissionRule.grant(Manifest.permission.RECORD_AUDIO)

    @Test
    fun continuousListeningForTheConfiguredWindow() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val context = instrumentation.targetContext
        val minutes = InstrumentationRegistry.getArguments()
            .getString("wakeWordBatteryMinutes")
            ?.toLongOrNull()
            ?: 60L
        val requestedMillis = minutes * 60_000L

        val controller = WakeWordController.create(context, onWakeWord = {})
        try {
            assertTrue("wake-word capture did not start", controller.start())
            waitForListening(controller, 30_000L)
            Thread.sleep(requestedMillis)
            controller.stop()

            val state = controller.state.value
            assertTrue(
                "expected Off or Stopping after stop, got $state",
                state is WakeWordState.Off || state is WakeWordState.Stopping,
            )
            assertTrue("AudioRecord was not released", controller.audioRecordReleased())
            assertFalse("capture thread is still alive", controller.captureThreadAlive())

            val summary = controller.writeUsageSummary(context.filesDir)
            Log.i(
                "WakeWordUsage",
                "listening_wall_ms=${summary.listeningWallMillis} " +
                    "cpu_ms=${summary.cpuMillis} " +
                    "detection_count=${summary.detectionCount}",
            )
            assertTrue("CPU millis must be non-zero", summary.cpuMillis > 0L)
            assertTrue(
                "listening window was ${summary.listeningWallMillis} ms, " +
                    "expected at least $requestedMillis ms",
                summary.listeningWallMillis >= requestedMillis - 1_000L,
            )
        } finally {
            controller.release()
        }
    }

    private fun waitForListening(controller: WakeWordController, timeoutMillis: Long) {
        val deadline = System.currentTimeMillis() + timeoutMillis
        while (System.currentTimeMillis() < deadline) {
            if (controller.state.value is WakeWordState.Listening) {
                return
            }
            Thread.sleep(100L)
        }
        throw AssertionError("wake word did not reach Listening: ${controller.state.value}")
    }
}
