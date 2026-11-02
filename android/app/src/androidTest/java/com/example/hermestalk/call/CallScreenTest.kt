package com.example.hermestalk.call

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import com.example.hermestalk.call.ui.CallScreen
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class CallScreenTest {

    @get:Rule
    val composeRule = createComposeRule()

    private fun render(
        uiState: CallUiState,
        onToggleMute: () -> Unit = {},
        onEndCall: () -> Unit = {},
        onSelectRoute: (AudioRoute) -> Unit = {},
        onRequestMicPermission: () -> Unit = {},
        onRequestBatteryExemption: () -> Unit = {},
        onRedial: () -> Unit = {},
        onBack: () -> Unit = {},
    ) {
        composeRule.setContent {
            CallScreen(
                uiState = uiState,
                onToggleMute = onToggleMute,
                onEndCall = onEndCall,
                onSelectRoute = onSelectRoute,
                onRequestMicPermission = onRequestMicPermission,
                onRequestBatteryExemption = onRequestBatteryExemption,
                onRedial = onRedial,
                onBack = onBack,
            )
        }
    }

    @Test
    fun activeCallShowsMuteButtonAndInvokesCallback() {
        var toggled = false
        render(
            uiState = CallUiState(
                state = CallState.Active,
                controls = CallControls(
                    levels = listOf(0.1f, 0.4f, 0.8f, 0.2f),
                    micActive = true,
                    audioFocusHeld = true,
                ),
            ),
            onToggleMute = { toggled = true },
        )

        composeRule.onNodeWithTag("call_visualiser").assertIsDisplayed()
        composeRule.onNodeWithTag("call_mute").assertIsDisplayed().performClick()
        assertTrue(toggled)
    }

    @Test
    fun permissionDeniedStateShowsPermissionCardWithoutCrashing() {
        render(
            uiState = CallUiState(
                state = CallState.Failed(
                    CallFailureReason.MicrophonePermissionDenied,
                    "Microphone permission denied",
                ),
                controls = CallControls(muted = true),
            ),
        )

        composeRule.onNodeWithTag("call_permission_denied").assertIsDisplayed()
    }

    @Test
    fun batteryPromptIsShownWhenOptimisationIsNeeded() {
        render(
            uiState = CallUiState(
                state = CallState.Active,
                controls = CallControls(
                    batteryOptimisationNeeded = true,
                    micActive = true,
                ),
            ),
        )

        composeRule.onNodeWithTag("call_battery_prompt").assertIsDisplayed()
    }

    @Test
    fun endButtonInvokesCallback() {
        var ended = false
        render(
            uiState = CallUiState(
                state = CallState.Active,
                controls = CallControls(micActive = true),
            ),
            onEndCall = { ended = true },
        )

        composeRule.onNodeWithTag("call_end").assertIsDisplayed().performClick()
        assertTrue(ended)
    }
}
