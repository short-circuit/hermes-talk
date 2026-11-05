package com.example.hermestalk.persistent

import android.Manifest
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.SystemClock
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.rule.GrantPermissionRule
import androidx.test.uiautomator.UiDevice
import com.example.hermestalk.MainActivity
import com.example.hermestalk.call.AssistantForegroundType
import com.example.hermestalk.call.PersistentAssistant
import com.example.hermestalk.call.PersistentAssistantController
import com.example.hermestalk.call.PersistentAssistantService
import com.example.hermestalk.call.SharedPreferencesPersistentAssistantSettingsStore
import com.example.hermestalk.wakeword.SharedPreferencesWakeWordSettingsStore
import com.example.hermestalk.wakeword.WakeWordSettings
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Instrumentation proof for the always-on assistant foreground service.
 *
 * The background-start test observes which foreground type the platform
 * actually allows. Under `am instrument` the target process is granted
 * background-activity-start privileges, so Android 14 may accept the
 * microphone type even after Home; a real background start falls back to
 * specialUse and records the refusal. The deterministic fallback itself is
 * covered by `PersistentAssistantPolicyTest`. The soak test defaults to 600
 * seconds and can be shortened through `-e persistentSoakSeconds <seconds>` or
 * the shared `-e screenOffSeconds <seconds>` argument the CI gate passes; the
 * service must survive both the screen-off and the forced-Doze window.
 */
@RunWith(AndroidJUnit4::class)
class PersistentAssistantServiceTest {

    @get:Rule
    val permissions: GrantPermissionRule =
        GrantPermissionRule.grant(Manifest.permission.RECORD_AUDIO)

    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context: Context = instrumentation.targetContext.applicationContext

    /**
     * Keep the app's automatic start/sync out of these service-level tests: the
     * ViewModel must not restart the service while a test is asserting on an
     * explicit controller call.
     */
    @Before
    fun disablePersistentAssistantAutoStart() {
        SharedPreferencesPersistentAssistantSettingsStore(context).setEnabled(false)
        SharedPreferencesWakeWordSettingsStore(context).save(
            WakeWordSettings(enabled = false),
        )
    }

    @Test
    fun backgroundStartUsesThePlatformAllowedForegroundType() {
        grantNotificationPermissionIfNeeded()
        val device = UiDevice.getInstance(instrumentation)
        device.pressHome()
        // Give the launcher a moment to take over the foreground task.
        SystemClock.sleep(BACKGROUND_SETTLE_MS)
        stopService()

        PersistentAssistantController.startFromForeground(context)

        await("foreground start", SERVICE_START_TIMEOUT_MS) {
            PersistentAssistant.state.value.running
        }
        val state = PersistentAssistant.state.value
        val hasRecordAudio = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED

        // Instrumentation grants the target process background-activity-start
        // privileges, so the microphone type can be accepted even after Home.
        // When the real background restriction fires, the service must record
        // the refusal and fall back to specialUse instead.
        val expectedType = when {
            state.micRefusal != null -> {
                assertTrue(
                    "a microphone refusal requires RECORD_AUDIO",
                    hasRecordAudio,
                )
                assertNotNull(
                    "the microphone refusal must be recorded",
                    state.micRefusal,
                )
                AssistantForegroundType.SpecialUse
            }

            hasRecordAudio -> AssistantForegroundType.Microphone
            else -> AssistantForegroundType.SpecialUse
        }
        assertEquals(expectedType, state.foregroundType)

        val dump = device.executeShellCommand(
            "dumpsys activity services ${context.packageName}",
        )
        when (expectedType) {
            AssistantForegroundType.Microphone -> assertTrue(
                "dumpsys must report the microphone type:\n$dump",
                dumpsysHasForegroundType(
                    dump,
                    "microphone",
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE,
                ),
            )

            AssistantForegroundType.SpecialUse -> assertTrue(
                "dumpsys must report the specialUse type:\n$dump",
                dumpsysHasForegroundType(
                    dump,
                    "specialUse",
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE,
                ),
            )

            AssistantForegroundType.None -> Unit
        }
        assertTrue("dumpsys must report isForeground=true:\n$dump", dump.contains("isForeground=true"))

        stopService()
    }

    @Test
    fun foregroundStartUsesMicrophoneTypeAndScopesTheWakeLock() {
        grantNotificationPermissionIfNeeded()
        val device = UiDevice.getInstance(instrumentation)
        ActivityScenario.launch(MainActivity::class.java).use {
            stopService()
            PersistentAssistantController.startFromForeground(context)

            await("microphone foreground type", SERVICE_START_TIMEOUT_MS) {
                val state = PersistentAssistant.state.value
                state.running && state.foregroundType == AssistantForegroundType.Microphone
            }
            assertEquals(
                AssistantForegroundType.Microphone,
                PersistentAssistant.state.value.foregroundType,
            )

            PersistentAssistantController.setListening(context, true)
            await("listening wake lock", WAKE_LOCK_TIMEOUT_MS) {
                PersistentAssistant.state.value.wakeLockHeld
            }
            assertTrue(
                "the wake lock must be held while listening",
                PersistentAssistant.state.value.wakeLockHeld,
            )

            PersistentAssistantController.setListening(context, false)
            await("released listening wake lock", WAKE_LOCK_TIMEOUT_MS) {
                !PersistentAssistant.state.value.wakeLockHeld
            }
            assertFalse(
                "the wake lock must be released when listening stops",
                PersistentAssistant.state.value.wakeLockHeld,
            )

            val dump = device.executeShellCommand(
                "dumpsys activity services ${context.packageName}",
            )
            assertTrue(
                "dumpsys must report the microphone type:\n$dump",
                dumpsysHasForegroundType(
                    dump,
                    "microphone",
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE,
                ),
            )

            stopService()
        }
    }

    @Test
    fun screenOffAndDozeKeepTheServiceAlive() {
        grantNotificationPermissionIfNeeded()
        val device = UiDevice.getInstance(instrumentation)
        val arguments = InstrumentationRegistry.getArguments()
        val soakSeconds = (arguments.getString("persistentSoakSeconds")
            ?: arguments.getString("screenOffSeconds"))
            ?.toLongOrNull()
            ?.coerceAtLeast(5L)
            ?: DEFAULT_SOAK_SECONDS

        ActivityScenario.launch(MainActivity::class.java).use {
            stopService()
            PersistentAssistantController.startFromForeground(context)
            await("microphone foreground type", SERVICE_START_TIMEOUT_MS) {
                val state = PersistentAssistant.state.value
                state.running && state.foregroundType == AssistantForegroundType.Microphone
            }

            val pidBefore = pidOf(device)
            assertTrue("could not read the app pid", pidBefore.isNotBlank())

            try {
                device.executeShellCommand("input keyevent 26")
                device.executeShellCommand("dumpsys deviceidle force-idle")
                SystemClock.sleep(soakSeconds * 1000L)

                val pidAfter = pidOf(device)
                assertEquals("the app process was replaced during doze", pidBefore, pidAfter)
                val state = PersistentAssistant.state.value
                assertTrue("the service stopped during doze", state.running)
                assertEquals(
                    "the service lost the microphone foreground type during doze",
                    AssistantForegroundType.Microphone,
                    state.foregroundType,
                )
                val dump = device.executeShellCommand(
                    "dumpsys activity services ${context.packageName}",
                )
                assertTrue(
                    "dumpsys must report the microphone type:\n$dump",
                    dumpsysHasForegroundType(
                        dump,
                        "microphone",
                        ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE,
                    ),
                )
            } finally {
                device.executeShellCommand("dumpsys deviceidle unforce")
                device.executeShellCommand("input keyevent 224")
                stopService()
            }
        }
    }

    @Test
    fun persistentServiceIsDeclaredStopWithTaskFalse() {
        val info = context.packageManager.getServiceInfo(
            ComponentName(context, PersistentAssistantService::class.java),
            0,
        )
        assertTrue(
            "stopWithTask must be false",
            info.flags and ServiceInfo.FLAG_STOP_WITH_TASK == 0,
        )
        assertTrue(
            "the service must declare the microphone foreground type",
            info.foregroundServiceType and
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE != 0,
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            assertTrue(
                "the service must declare the specialUse foreground type",
                info.foregroundServiceType and
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE != 0,
            )
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

    private fun stopService() {
        context.stopService(Intent(context, PersistentAssistantService::class.java))
        await("service stopped", SERVICE_STOP_TIMEOUT_MS) {
            !PersistentAssistant.state.value.running
        }
    }

    private fun pidOf(device: UiDevice): String =
        device.executeShellCommand("pidof ${context.packageName}").trim()

    /**
     * `dumpsys activity services` prints the foreground type as a bit mask
     * (`types=00000080`) on API 34, while some builds also print the symbolic
     * name. Accept either representation.
     */
    private fun dumpsysHasForegroundType(
        dump: String,
        symbolicName: String,
        type: Int,
    ): Boolean {
        if (dump.contains(symbolicName, ignoreCase = true)) {
            return true
        }
        return Regex("types=([0-9a-fA-F]+)")
            .findAll(dump)
            .mapNotNull { it.groupValues[1].toLongOrNull(16) }
            .any { (it and type.toLong()) != 0L }
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
        const val DEFAULT_SOAK_SECONDS = 600L
        const val BACKGROUND_SETTLE_MS = 2_000L
        const val SERVICE_START_TIMEOUT_MS = 20_000L
        const val SERVICE_STOP_TIMEOUT_MS = 10_000L
        const val WAKE_LOCK_TIMEOUT_MS = 10_000L
        const val POLL_INTERVAL_MS = 100L
    }
}
