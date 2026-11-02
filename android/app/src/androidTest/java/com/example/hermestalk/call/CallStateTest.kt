package com.example.hermestalk.call

import android.Manifest
import android.content.Context
import android.media.AudioManager
import android.os.SystemClock
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.rule.GrantPermissionRule
import com.example.hermestalk.MainActivity
import com.example.hermestalk.rtvi.RelaySettings
import java.net.InetAddress
import java.net.ServerSocket
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Call-state coverage on a real device: an unreachable relay must move the call
 * through Reconnecting to Failed and release the foreground service, the
 * microphone and the audio mode, while control actions on an idle call must be
 * no-ops rather than crashes.
 *
 * The microphone permission is granted by the rule; denial is covered by
 * [CallPermissionTest] and the rendered state by [CallScreenTest].
 */
@RunWith(AndroidJUnit4::class)
class CallStateTest {

    @get:Rule
    val permissions: GrantPermissionRule = GrantPermissionRule.grant(Manifest.permission.RECORD_AUDIO)

    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context: Context = instrumentation.targetContext.applicationContext
    private val audioManager: AudioManager by lazy {
        context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    }

    private lateinit var controller: CallController

    @Before
    fun setUp() {
        controller = CallControllers.get(context)
        if (controller.uiState.value.state.isLive) {
            instrumentation.runOnMainSync { controller.endCall() }
            await("a call left over from an earlier test to end", END_TIMEOUT_MS) {
                !controller.uiState.value.state.isLive
            }
        }
    }

    @Test
    fun unreachableRelayFailsTheCallAndReleasesEverything() {
        val unreachable = "http://127.0.0.1:${closedLoopbackPort()}"
        ActivityScenario.launch(MainActivity::class.java).use {
            instrumentation.runOnMainSync {
                controller.startCall(RelaySettings(relayUrl = unreachable, deviceToken = TOKEN))
            }
            await("the call to leave idle", START_TIMEOUT_MS) {
                controller.uiState.value.state !is CallState.Idle
            }

            // A call fails on the first unreachable probe: the call-level state
            // machine cleans up instead of retrying, and the retry budget lives
            // on the connection screen.
            await("the call to fail", FAILURE_TIMEOUT_MS) {
                controller.uiState.value.state is CallState.Failed
            }
            val failed = controller.uiState.value.state
            assertTrue("expected a failed call, got ${failed.describe()}", failed is CallState.Failed)
            assertEquals(
                CallFailureReason.RelayUnreachable,
                (failed as CallState.Failed).reason,
            )

            await("the foreground service to stop", SERVICE_TIMEOUT_MS) {
                !CallForegroundService.isRunning()
            }
            assertFalse(CallForegroundService.isRunning())
            assertEquals(0, CallForegroundService.foregroundType())
            assertFalse(controller.uiState.value.controls.micActive)
            await("the audio mode to be restored", AUDIO_TIMEOUT_MS) {
                audioManager.mode == AudioManager.MODE_NORMAL
            }
            assertEquals(AudioManager.MODE_NORMAL, audioManager.mode)
        }
    }

    @Test
    fun controlActionsAreIgnoredWhileNoCallIsLive() {
        val before = controller.uiState.value
        assertFalse("this test needs a call that is not live", before.state.isLive)
        instrumentation.runOnMainSync {
            controller.toggleMute()
            controller.endCall()
        }
        assertEquals(
            "toggleMute and endCall must not move a call that is not live",
            before.state,
            controller.uiState.value.state,
        )
        assertEquals(before.controls.muted, controller.uiState.value.controls.muted)
        assertFalse(CallForegroundService.isRunning())
    }

    private fun closedLoopbackPort(): Int {
        val socket = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))
        val port = socket.localPort
        socket.close()
        return port
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
        const val TOKEN = "stub-token"
        const val START_TIMEOUT_MS = 10_000L
        const val FAILURE_TIMEOUT_MS = 30_000L
        const val END_TIMEOUT_MS = 20_000L
        const val SERVICE_TIMEOUT_MS = 15_000L
        const val AUDIO_TIMEOUT_MS = 10_000L
        const val POLL_INTERVAL_MS = 100L
    }
}
