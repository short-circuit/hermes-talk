package com.example.hermestalk

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.example.hermestalk.call.CallUiState
import com.example.hermestalk.call.ui.CallScreen
import com.example.hermestalk.ui.ConnectionScreen
import com.example.hermestalk.ui.HermesTalkTheme
import com.example.hermestalk.ui.SettingsScreen

private enum class AppScreen { Settings, Connection, Call }

/**
 * Entry point. The microphone permission is requested only when the user starts
 * a connection or a call; denial is non-fatal. The call screen owns the richer
 * microphone UX, including a permission card and a battery-optimisation prompt.
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            val appViewModel: AppViewModel = viewModel()
            val uiState by appViewModel.uiState.collectAsStateWithLifecycle()
            val callState by appViewModel.callState.collectAsStateWithLifecycle()
            var screen by rememberSaveable { mutableStateOf(AppScreen.Settings) }

            val permissionLauncher = rememberLauncherForActivityResult(
                ActivityResultContracts.RequestPermission(),
            ) { granted ->
                appViewModel.setMicPermissionDenied(!granted)
                appViewModel.connect()
            }

            val callPermissionLauncher = rememberLauncherForActivityResult(
                ActivityResultContracts.RequestPermission(),
            ) { granted ->
                appViewModel.onCallMicPermissionResult(granted)
            }

            val wakeWordPermissionLauncher = rememberLauncherForActivityResult(
                ActivityResultContracts.RequestPermission(),
            ) { granted ->
                appViewModel.onWakeWordPermissionResult(granted)
            }

            val batteryLauncher = rememberLauncherForActivityResult(
                ActivityResultContracts.StartActivityForResult(),
            ) {
                // The system settings screen reports its result in its own UI.
            }

            val requestConnect: () -> Unit = {
                if (ContextCompat.checkSelfPermission(
                        this,
                        Manifest.permission.RECORD_AUDIO,
                    ) == PackageManager.PERMISSION_GRANTED
                ) {
                    appViewModel.setMicPermissionDenied(false)
                    appViewModel.connect()
                } else {
                    permissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
                }
            }

            val requestCallMicPermission: () -> Unit = {
                if (ContextCompat.checkSelfPermission(
                        this,
                        Manifest.permission.RECORD_AUDIO,
                    ) == PackageManager.PERMISSION_GRANTED
                ) {
                    appViewModel.onCallMicPermissionResult(true)
                } else {
                    callPermissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
                }
            }

            val requestBatteryExemption: () -> Unit = {
                appViewModel.requestBatteryExemption(this)?.let { intent ->
                    batteryLauncher.launch(intent)
                }
            }

            val setWakeWordEnabled: (Boolean) -> Unit = { enabled ->
                if (
                    enabled &&
                    ContextCompat.checkSelfPermission(
                        this,
                        Manifest.permission.RECORD_AUDIO,
                    ) != PackageManager.PERMISSION_GRANTED
                ) {
                    wakeWordPermissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
                } else {
                    appViewModel.setWakeWordEnabled(enabled)
                }
            }

            HermesTalkTheme {
                AppRoot(
                    uiState = uiState,
                    callState = callState,
                    screen = screen,
                    onNavigate = { screen = it },
                    viewModel = appViewModel,
                    requestConnect = requestConnect,
                    requestCallMicPermission = requestCallMicPermission,
                    requestBatteryExemption = requestBatteryExemption,
                    setWakeWordEnabled = setWakeWordEnabled,
                )
            }
        }
    }
}

@Composable
private fun AppRoot(
    uiState: AppUiState,
    callState: CallUiState,
    screen: AppScreen,
    onNavigate: (AppScreen) -> Unit,
    viewModel: AppViewModel,
    requestConnect: () -> Unit,
    requestCallMicPermission: () -> Unit,
    requestBatteryExemption: () -> Unit,
    setWakeWordEnabled: (Boolean) -> Unit,
) {
    when (screen) {
        AppScreen.Settings -> SettingsScreen(
            uiState = uiState,
            onSave = { url, token, tlsPin, trustPlatformCa ->
                viewModel.saveSettings(url, token, tlsPin, trustPlatformCa)
            },
            onConnect = { url, token, tlsPin, trustPlatformCa ->
                if (viewModel.saveSettings(url, token, tlsPin, trustPlatformCa)) {
                    onNavigate(AppScreen.Connection)
                    requestConnect()
                }
            },
            onFetchFingerprint = viewModel::fetchFingerprint,
            onToggleTokenVisibility = viewModel::toggleTokenVisibility,
        )

        AppScreen.Connection -> ConnectionScreen(
            uiState = uiState,
            onConnect = requestConnect,
            onDisconnect = viewModel::disconnect,
            onRetry = requestConnect,
            onStartCall = {
                onNavigate(AppScreen.Call)
                viewModel.startCall()
            },
            onBack = { onNavigate(AppScreen.Settings) },
            onWakeWordToggle = setWakeWordEnabled,
            onWakeWordSensitivity = viewModel::setWakeWordSensitivity,
            onPushToTalkPressed = viewModel::pushToTalkPressed,
            onPushToTalkReleased = viewModel::pushToTalkReleased,
        )

        AppScreen.Call -> CallScreen(
            uiState = callState,
            onToggleMute = viewModel::toggleCallMute,
            onEndCall = viewModel::endCall,
            onSelectRoute = viewModel::selectCallRoute,
            onRequestMicPermission = requestCallMicPermission,
            onRequestBatteryExemption = requestBatteryExemption,
            onRedial = viewModel::startCall,
            onBack = {
                viewModel.endCall()
                onNavigate(AppScreen.Connection)
            },
        )
    }
}
