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
import com.example.hermestalk.ui.ConnectionScreen
import com.example.hermestalk.ui.HermesTalkTheme
import com.example.hermestalk.ui.SettingsScreen

private enum class AppScreen { Settings, Connection }

/**
 * Entry point. The microphone permission is requested only when the user starts
 * a connection; denial is non-fatal, the session connects with a silent mic
 * track and the UI shows a notice. The call-UI card owns richer mic UX.
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            val appViewModel: AppViewModel = viewModel()
            val uiState by appViewModel.uiState.collectAsStateWithLifecycle()
            var screen by rememberSaveable { mutableStateOf(AppScreen.Settings) }

            val permissionLauncher = rememberLauncherForActivityResult(
                ActivityResultContracts.RequestPermission(),
            ) { granted ->
                appViewModel.setMicPermissionDenied(!granted)
                appViewModel.connect()
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

            HermesTalkTheme {
                AppRoot(
                    uiState = uiState,
                    screen = screen,
                    onNavigate = { screen = it },
                    viewModel = appViewModel,
                    requestConnect = requestConnect,
                )
            }
        }
    }
}

@Composable
private fun AppRoot(
    uiState: AppUiState,
    screen: AppScreen,
    onNavigate: (AppScreen) -> Unit,
    viewModel: AppViewModel,
    requestConnect: () -> Unit,
) {
    when (screen) {
        AppScreen.Settings -> SettingsScreen(
            uiState = uiState,
            onSave = { url, token -> viewModel.saveSettings(url, token) },
            onConnect = { url, token ->
                if (viewModel.saveSettings(url, token)) {
                    onNavigate(AppScreen.Connection)
                    requestConnect()
                }
            },
            onToggleTokenVisibility = viewModel::toggleTokenVisibility,
        )

        AppScreen.Connection -> ConnectionScreen(
            uiState = uiState,
            onConnect = requestConnect,
            onDisconnect = viewModel::disconnect,
            onRetry = requestConnect,
            onBack = { onNavigate(AppScreen.Settings) },
        )
    }
}
