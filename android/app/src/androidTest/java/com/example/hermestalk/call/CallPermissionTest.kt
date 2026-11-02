package com.example.hermestalk.call

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioManager
import android.os.SystemClock
import androidx.core.content.ContextCompat
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.rule.GrantPermissionRule
import com.example.hermestalk.MainActivity
import com.example.hermestalk.rtvi.RelaySettings
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Verifies that a microphone permission denial fails the call cleanly instead
 * of crashing, and that the foreground service and the audio path are torn
 * down.
 *
 * The denial is fed through [CallController.onMicPermissionResult], which is
 * exactly what the activity does when the permission launcher returns false.
 * Revoking the permission with `UiAutomation` is not used here because it kills
 * the instrumented process (the runner lives in the app process).
 */
@RunWith(AndroidJUnit4::class)
class CallPermissionTest {

    @get:Rule
    val permissions: GrantPermissionRule =
        GrantPermissionRule.grant(Manifest.permission.RECORD_AUDIO)

    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context: Context = instrumentation.targetContext.applicationContext

    @Test
    fun deniedPermissionFailsCleanlyAndStopsTheService() {
        val controller = CallControllers.get(context)
        val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        assertTrue(
            "the microphone permission must be granted for the call to start",
            ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.RECORD_AUDIO,
            ) == PackageManager.PERMISSION_GRANTED,
        )

        ActivityScenario.launch(MainActivity::class.java).use {
            instrumentation.runOnMainSync {
                controller.startCall(RelaySettings("http://127.0.0.1:8790", ""))
            }
            await("call to leave idle", CALL_START_TIMEOUT_MS) {
                controller.uiState.value.state !is CallState.Idle
            }
            assertFalse(
                "a granted permission must not wait for the launcher",
                controller.uiState.value.state is CallState.AwaitingMicPermission,
            )

            instrumentation.runOnMainSync {
                controller.onMicPermissionResult(false)
            }
            await("failed call", FAILURE_TIMEOUT_MS) {
                controller.uiState.value.state is CallState.Failed
            }

            val state = controller.uiState.value.state
            assertTrue("expected a failed call, got ${state.describe()}", state is CallState.Failed)
            assertEquals(
                CallFailureReason.MicrophonePermissionDenied,
                (state as CallState.Failed).reason,
            )
            await("service stopped", SERVICE_STOP_TIMEOUT_MS) {
                !CallForegroundService.isRunning()
            }
            assertFalse(CallForegroundService.isRunning())
            assertEquals(0, CallForegroundService.foregroundType())
            assertFalse(controller.uiState.value.controls.micActive)
            await("audio mode restored", AUDIO_RESTORE_TIMEOUT_MS) {
                audioManager.mode == AudioManager.MODE_NORMAL
            }
            assertEquals(AudioManager.MODE_NORMAL, audioManager.mode)
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
        const val CALL_START_TIMEOUT_MS = 10_000L
        const val FAILURE_TIMEOUT_MS = 10_000L
        const val SERVICE_STOP_TIMEOUT_MS = 10_000L
        const val AUDIO_RESTORE_TIMEOUT_MS = 5_000L
        const val POLL_INTERVAL_MS = 100L
    }
}
