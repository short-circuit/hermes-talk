package com.example.hermestalk.assistant

import android.Manifest
import android.app.role.RoleManager
import android.os.Build
import android.os.ParcelFileDescriptor
import android.os.SystemClock
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.rule.GrantPermissionRule
import androidx.test.uiautomator.By
import androidx.test.uiautomator.UiDevice
import com.example.hermestalk.AssistantRole
import com.example.hermestalk.AssistantRoleState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * CI gate for the system assistant role and the hardware assist key.
 *
 * The role is a user-controlled privilege, so a headless emulator cannot grant
 * it through the system dialog. This test grants it through the shell interface
 * (`cmd role add-role-holder`), then proves two things against the platform
 * rather than against the app's own bookkeeping: the role holder list names this
 * package, and the assist key (`KEYCODE_ASSIST`, keycode 219) dispatches to the
 * app and lands on the call screen.
 *
 * It FAILS, it does not skip, when the role cannot be obtained or the assist key
 * does not reach the app: a skipped role gate would claim coverage it never had.
 * The role is available from API 29; on older devices the platform cannot hold
 * it and the gate is reported as unavailable (the CI image is API 34).
 */
@RunWith(AndroidJUnit4::class)
class AssistantRoleHeldTest {

    @get:Rule
    val permissions: GrantPermissionRule = GrantPermissionRule.grant(
        Manifest.permission.RECORD_AUDIO,
    )

    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val device = UiDevice.getInstance(instrumentation)
    private val context = instrumentation.targetContext.applicationContext

    @Test
    fun assistantRoleIsHeldByThisPackage() {
        ensureAssistantRole()
        val holders = shell("cmd role get-role-holders $ASSISTANT_ROLE")
        assertTrue(
            "the platform role holder list must name ${context.packageName}, got: $holders",
            holders.lineSequence().any { it.trim() == context.packageName },
        )
        assertEquals(
            "the app must report the role as held once the platform does",
            AssistantRoleState.Held,
            AssistantRole.state(context),
        )
    }

    @Test
    fun assistKeyOpensTheAssistantOnTheCallScreen() {
        ensureAssistantRole()

        device.wakeUp()
        SystemClock.sleep(WAKE_SETTLE_MS)
        device.pressHome()
        SystemClock.sleep(LAUNCHER_SETTLE_MS)

        // The exact command CI runs: the platform dispatches the assist key to
        // the role holder, which must be this app.
        shell("input keyevent $KEYCODE_ASSIST")

        await("MainActivity resumed by the assist key", ASSIST_TIMEOUT_MS) {
            val dump = shell("dumpsys activity activities")
            dump.lineSequence().any { line ->
                line.contains("topResumedActivity=") &&
                    line.contains("${context.packageName}/.MainActivity")
            }
        }
        awaitCallScreen()

        val resumed = shell("dumpsys activity activities")
        assertTrue(
            "the assist key must leave MainActivity resumed, dump:\n$resumed",
            resumed.lineSequence().any { line ->
                line.contains("topResumedActivity=") &&
                    line.contains("${context.packageName}/.MainActivity")
            },
        )
    }

    /**
     * Grant the role if the platform does not hold it yet. `cmd role
     * add-role-holder` is idempotent and runs as the shell user, so the
     * headless emulator needs no dialog. The assertion afterwards proves the
     * grant took effect instead of trusting the command's exit code.
     */
    private fun ensureAssistantRole() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            fail("the assistant role requires API 29; this device is API ${Build.VERSION.SDK_INT}")
        }
        if (AssistantRole.state(context) != AssistantRoleState.Held) {
            shell("cmd role add-role-holder $ASSISTANT_ROLE ${context.packageName}")
        }
        val roleManager = context.getSystemService(RoleManager::class.java)
            ?: error("RoleManager is unavailable on this device")
        assertTrue(
            "the assistant role must be available on this device",
            roleManager.isRoleAvailable(RoleManager.ROLE_ASSISTANT),
        )
        assertEquals(
            "the assistant role was not granted to ${context.packageName}; " +
                "run `adb shell cmd role add-role-holder $ASSISTANT_ROLE ${context.packageName}`",
            AssistantRoleState.Held,
            AssistantRole.state(context),
        )
    }

    /**
     * Wait for the Compose call-screen root. Depending on the platform, the
     * `testTagsAsResourceId` tag is exposed either as the bare `call_screen`
     * resource id or as `<package>:id/call_screen`; accept both forms.
     */
    private fun awaitCallScreen() {
        val byBareTag = By.res("call_screen")
        val byPackageTag = By.res(context.packageName, "call_screen")
        await("the call screen", CALL_SCREEN_TIMEOUT_MS) {
            device.findObject(byBareTag) != null || device.findObject(byPackageTag) != null
        }
    }

    private fun shell(command: String): String =
        instrumentation.uiAutomation.executeShellCommand(command).use { descriptor ->
            ParcelFileDescriptor.AutoCloseInputStream(descriptor)
                .bufferedReader()
                .readText()
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
        const val ASSISTANT_ROLE = "android.app.role.ASSISTANT"
        const val KEYCODE_ASSIST = 219
        const val WAKE_SETTLE_MS = 1_000L
        const val LAUNCHER_SETTLE_MS = 2_000L
        const val ASSIST_TIMEOUT_MS = 20_000L
        const val CALL_SCREEN_TIMEOUT_MS = 20_000L
        const val POLL_INTERVAL_MS = 100L
    }
}
