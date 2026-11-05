package com.example.hermestalk.assistant

import android.Manifest
import android.app.role.RoleManager
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.SystemClock
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createEmptyComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.lifecycle.Lifecycle
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.rule.GrantPermissionRule
import com.example.hermestalk.AssistantRole
import com.example.hermestalk.AssistantRoleState
import com.example.hermestalk.MainActivity
import com.example.hermestalk.call.CallControllers
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * Verifies the assist entry point and the assistant-role query.
 *
 * The assist flow is independent of the relay: the activity must resume on the
 * call screen even when the relay settings do not validate and the call stays
 * idle, or when the call fails as `RelayUnreachable`.
 */
@RunWith(AndroidJUnit4::class)
class AssistantRoleTest {

    @get:Rule(order = 0)
    val permissions: GrantPermissionRule = GrantPermissionRule.grant(
        Manifest.permission.RECORD_AUDIO,
    )

    @get:Rule(order = 1)
    val composeRule = createEmptyComposeRule()

    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context: Context = instrumentation.targetContext.applicationContext

    @Test
    fun assistIntentResumesTheActivityOnTheCallScreen() {
        val intent = Intent(context, MainActivity::class.java).apply {
            action = Intent.ACTION_ASSIST
            addCategory(Intent.CATEGORY_DEFAULT)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }

        val scenario = ActivityScenario.launch<MainActivity>(intent)
        try {
            val deadline = SystemClock.uptimeMillis() + 10_000
            while (
                scenario.state != Lifecycle.State.RESUMED &&
                SystemClock.uptimeMillis() < deadline
            ) {
                SystemClock.sleep(100)
            }
            assertEquals(Lifecycle.State.RESUMED, scenario.state)
            composeRule.waitUntil(10_000) {
                runCatching {
                    composeRule.onNodeWithTag("call_visualiser").assertIsDisplayed()
                }.isSuccess
            }
            composeRule.onNodeWithTag("call_visualiser").assertIsDisplayed()
        } finally {
            instrumentation.runOnMainSync {
                CallControllers.get(context).endCall()
            }
            scenario.close()
        }
    }

    @Test
    fun assistantRoleStateQueryDoesNotThrow() {
        val state = AssistantRole.state(context)
        assertTrue(
            "the reported state must be one of the enum values",
            state in AssistantRoleState.entries,
        )

        val request = AssistantRole.requestIntent(context)
        if (state == AssistantRoleState.Unavailable) {
            // No role request is possible on this device.
        } else {
            assertNotNull(
                "an available role must produce a request intent",
                request,
            )
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val roleManager = context.getSystemService(RoleManager::class.java)
            val held = roleManager?.isRoleHeld(RoleManager.ROLE_ASSISTANT) == true
            assertEquals(
                "the reported Held state must match the platform role holder",
                held,
                state == AssistantRoleState.Held,
            )
        }
    }
}
