package com.example.hermestalk.call

import android.Manifest
import android.app.Notification
import android.app.NotificationManager
import android.content.Context
import android.content.pm.ServiceInfo
import android.media.AudioManager
import android.media.MediaRecorder
import android.os.Build
import android.os.SystemClock
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.rule.GrantPermissionRule
import androidx.test.uiautomator.UiDevice
import com.example.hermestalk.MainActivity
import com.example.hermestalk.rtvi.RelaySettings
import java.net.HttpURLConnection
import java.net.URL
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * End-to-end instrumentation coverage for the microphone foreground service.
 *
 * The test needs a reachable stub relay at `http://127.0.0.1:8790` (see
 * `android/README.md`). When the relay is not reachable the test still verifies
 * that the call fails cleanly and releases the service.
 *
 * `screenOffSeconds` defaults to 600 and can be overridden with
 * `-e screenOffSeconds <seconds>` when running through `adb shell am instrument`.
 */
@RunWith(AndroidJUnit4::class)
class CallForegroundServiceTest {

    @get:Rule
    val permissions: GrantPermissionRule =
        GrantPermissionRule.grant(Manifest.permission.RECORD_AUDIO)

    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context: Context = instrumentation.targetContext.applicationContext

    @Test
    fun callSurvivesScreenOffAndReleasesTheMicrophoneWhenEnded() {
        grantNotificationPermissionIfNeeded()
        val screenOffSeconds = InstrumentationRegistry.getArguments()
            .getString("screenOffSeconds")
            ?.toLongOrNull()
            ?.coerceAtLeast(5L)
            ?: DEFAULT_SCREEN_OFF_SECONDS
        val controller = CallControllers.get(context)
        val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val recordingsBefore = recordingSources(audioManager)
        val relayUp = relayHealthy()

        ActivityScenario.launch(MainActivity::class.java).use {
            startCall(controller)

            if (!relayUp) {
                await("terminal relay failure", RELAY_FAILURE_TIMEOUT_MS) {
                    controller.uiState.value.state is CallState.Failed
                }
                val state = controller.uiState.value.state
                assertTrue("expected a failed call, got ${state.describe()}", state is CallState.Failed)
                await("service stopped after failure", SERVICE_STOP_TIMEOUT_MS) {
                    !CallForegroundService.isRunning()
                }
                assertFalse(CallForegroundService.isRunning())
                assertEquals(0, CallForegroundService.foregroundType())
                return@use
            }

            await("active call", CALL_START_TIMEOUT_MS) {
                controller.uiState.value.state is CallState.Active
            }
            assertEquals(CallState.Active, controller.uiState.value.state)
            assertTrue(CallForegroundService.isRunning())
            assertEquals(
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE,
                CallForegroundService.foregroundType(),
            )

            val notification = awaitNotification()
            val actions = notification.actions ?: emptyArray()
            assertEquals("mute and end actions", 2, actions.size)
            assertTrue(actions.all { it.title.toString().isNotBlank() })

            val heartbeatBefore = controller.heartbeat
            val device = UiDevice.getInstance(instrumentation)
            try {
                device.sleep()
                SystemClock.sleep(screenOffSeconds * 1000L)
                val heartbeatAfter = controller.heartbeat
                assertTrue(
                    "timer stopped while the screen was off: " +
                        "$heartbeatBefore -> $heartbeatAfter",
                    heartbeatAfter > heartbeatBefore,
                )
                assertTrue(
                    "expected about $screenOffSeconds timer ticks while the screen was off, " +
                        "got ${heartbeatAfter - heartbeatBefore}",
                    heartbeatAfter - heartbeatBefore >= screenOffSeconds - TIMER_TOLERANCE_SECONDS,
                )
                val state = controller.uiState.value.state
                assertTrue(
                    "call did not survive the screen being off: ${state.describe()}",
                    state is CallState.Active || state is CallState.Reconnecting,
                )
            } finally {
                device.wakeUp()
            }

            endCall(controller)
            await("ended call", END_TIMEOUT_MS) {
                controller.uiState.value.state is CallState.Ended
            }
            assertTrue(controller.uiState.value.state is CallState.Ended)
            await("service stopped after ending", SERVICE_STOP_TIMEOUT_MS) {
                !CallForegroundService.isRunning()
            }
            assertFalse(CallForegroundService.isRunning())
            assertEquals(0, CallForegroundService.foregroundType())

            await("microphone released", MIC_RELEASE_TIMEOUT_MS) {
                recordingSources(audioManager).size <= recordingsBefore.size
            }
            assertTrue(
                "microphone still recording after the call ended",
                recordingSources(audioManager).size <= recordingsBefore.size,
            )
        }
    }

    private fun startCall(controller: CallController) {
        instrumentation.runOnMainSync {
            controller.startCall(RelaySettings("http://127.0.0.1:8790", ""))
        }
    }

    private fun endCall(controller: CallController) {
        instrumentation.runOnMainSync {
            controller.endCall()
        }
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

    private fun relayHealthy(): Boolean = try {
        val connection = URL("http://127.0.0.1:8790/health")
            .openConnection() as HttpURLConnection
        connection.connectTimeout = RELAY_PROBE_TIMEOUT_MS.toInt()
        connection.readTimeout = RELAY_PROBE_TIMEOUT_MS.toInt()
        connection.requestMethod = "GET"
        val code = connection.responseCode
        connection.disconnect()
        code in 200..399
    } catch (_: Exception) {
        false
    }

    private fun awaitNotification(): Notification {
        val manager =
            context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        var found: Notification? = null
        await("foreground notification", NOTIFICATION_TIMEOUT_MS) {
            found = manager.activeNotifications
                .firstOrNull { it.id == CallForegroundService.NOTIFICATION_ID }
                ?.notification
            found != null
        }
        if (found == null) {
            fail("foreground notification was not posted")
        }
        return found!!
    }

    private fun recordingSources(audioManager: AudioManager): Set<Int> = try {
        audioManager.activeRecordingConfigurations
            .map { it.clientAudioSource }
            .filter {
                it == MediaRecorder.AudioSource.VOICE_COMMUNICATION ||
                    it == MediaRecorder.AudioSource.MIC
            }
            .toSet()
    } catch (_: Exception) {
        emptySet()
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
        const val DEFAULT_SCREEN_OFF_SECONDS = 600L
        const val TIMER_TOLERANCE_SECONDS = 5L
        const val RELAY_PROBE_TIMEOUT_MS = 2_000L
        const val CALL_START_TIMEOUT_MS = 20_000L
        const val RELAY_FAILURE_TIMEOUT_MS = 45_000L
        const val END_TIMEOUT_MS = 10_000L
        const val SERVICE_STOP_TIMEOUT_MS = 10_000L
        const val MIC_RELEASE_TIMEOUT_MS = 5_000L
        const val NOTIFICATION_TIMEOUT_MS = 10_000L
        const val POLL_INTERVAL_MS = 100L
    }
}
