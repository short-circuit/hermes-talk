package com.example.hermestalk.assistant

import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performScrollTo
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.example.hermestalk.AppUiState
import com.example.hermestalk.AssistantRoleState
import com.example.hermestalk.ui.HermesTalkTheme
import com.example.hermestalk.ui.SettingsScreen
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** Renders the settings card for every assistant-role state. */
@RunWith(AndroidJUnit4::class)
class AssistantRoleSettingsScreenTest {

    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun settingsScreenRendersEveryAssistantRoleStateWithoutCrashing() {
        val roleState = mutableStateOf(AssistantRoleState.Held)
        val roleRequestRefused = mutableStateOf(false)

        composeRule.setContent {
            HermesTalkTheme {
                SettingsScreen(
                    uiState = AppUiState(),
                    onSave = { _, _, _, _ -> true },
                    onConnect = { _, _, _, _ -> },
                    onFetchFingerprint = {},
                    onToggleTokenVisibility = {},
                    assistantRole = roleState.value,
                    assistantRoleRequestRefused = roleRequestRefused.value,
                    onRequestAssistantRole = {},
                )
            }
        }

        composeRule.onNodeWithTag("settings_assistant_role")
            .performScrollTo()
            .assertIsDisplayed()

        composeRule.runOnIdle { roleState.value = AssistantRoleState.NotHeld }
        composeRule.onNodeWithTag("settings_assistant_role")
            .performScrollTo()
            .assertIsDisplayed()
        composeRule.onNodeWithTag("settings_assistant_role_button")
            .performScrollTo()
            .assertIsDisplayed()

        composeRule.runOnIdle { roleRequestRefused.value = true }
        composeRule.onNodeWithTag("settings_assistant_role_refused")
            .performScrollTo()
            .assertIsDisplayed()

        composeRule.runOnIdle { roleState.value = AssistantRoleState.Unavailable }
        composeRule.onNodeWithTag("settings_assistant_role")
            .performScrollTo()
            .assertIsDisplayed()
    }
}
