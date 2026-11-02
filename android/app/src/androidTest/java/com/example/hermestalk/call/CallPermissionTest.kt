package com.example.hermestalk.call

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.SystemClock
import androidx.core.content.ContextCompat
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.example.hermestalk.MainActivity
import com.example.hermestalk.rtvi.RelaySettings
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Verifies that a denied microphone permission fails the call before the
 * foreground service starts and that the controller does not crash.
 */
@RunWith(AndroidJUnit4::class)
class CallPermissionTest {

    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context: Context = instrumentation.targetContext.applicationContext

    @Before
    fun revokeMicrophonePermission() {
        instrumentation.uiAutomation.revokeRuntimePermission(
            context.packageName,
            Manifest.permission.RECORD_AUDIO,
        )
        instrumentation.waitForIdleSync()
        await("microphone permission to be revoked", PERMISSION_TIMEOUT_MS) {
            ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.RECORD_AUDIO,
            ) != PackageManager.PERMISSION_GRANTED
        }
    }

    @After
    fun restoreMicrophonePermission() {
        try {
            instrumentation.uiAutomation.grantRuntimePermission(
                context.packageName,
                Manifest.permission.RECORD_AUDIO,
            )
        } catch (_: Exception) {
            // A subsequent test can grant it again if this fails.
        }
    }

    @Test
    fun deniedPermissionFailsWithoutStartingTheService() {
        val controller = CallControllers.get(context)

        ActivityScenario.launch(MainActivity::class.java).use {
            instrumentation.runOnMainSync {
                controller.startCall(RelaySettings("http://127.0.0.1:8790", ""))
            }

            val awaiting = controller.uiState.value.state
            assertTrue(
                "expected AwaitingMicPermission, got ${awaiting.describe()}",
                awaiting is CallState.AwaitingMicPermission,
            )
            assertFalse(CallForegroundService.isRunning())

            instrumentation.runOnMainSync {
                controller.onMicPermissionResult(false)
            }

            val state = controller.uiState.value.state
            assertTrue("expected a failed call, got ${state.describe()}", state is CallState.Failed)
            assertEquals(
                CallFailureReason.MicrophonePermissionDenied,
                (state as CallState.Failed).reason,
            )
            assertFalse(CallForegroundService.isRunning())
            assertEquals(0, CallForegroundService.foregroundType())
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
        fail("timed out waiting for $label")
    }

    private companion object {
        const val PERMISSION_TIMEOUT_MS = 5_000L
        const val POLL_INTERVAL_MS = 100L
    }
}
