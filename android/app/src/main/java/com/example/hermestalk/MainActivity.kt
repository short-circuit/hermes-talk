package com.example.hermestalk

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.example.hermestalk.attachments.ui.AttachmentPanel
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

    private val pendingShare = mutableStateOf<Intent?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        handleShareIntent(intent)
        setContent {
            val appViewModel: AppViewModel = viewModel()
            val uiState by appViewModel.uiState.collectAsStateWithLifecycle()
            val callState by appViewModel.callState.collectAsStateWithLifecycle()
            val uploads by appViewModel.uploadItems.collectAsStateWithLifecycle()
            val artifactState by appViewModel.artifactState.collectAsStateWithLifecycle()
            val attachmentError by appViewModel.attachmentError.collectAsStateWithLifecycle()
            var screen by rememberSaveable { mutableStateOf(AppScreen.Settings) }

            val sharedIntent by pendingShare
            LaunchedEffect(sharedIntent) {
                val share = sharedIntent ?: return@LaunchedEffect
                if (screen == AppScreen.Settings) {
                    screen = AppScreen.Connection
                }
                appViewModel.attachFromIntent(share)
                pendingShare.value = null
            }

            val attachmentPicker = rememberLauncherForActivityResult(
                ActivityResultContracts.OpenDocument(),
            ) { uri ->
                uri?.let { appViewModel.pickAttachment(it) }
            }
            val pickAttachment: () -> Unit = {
                attachmentPicker.launch(arrayOf("*/*"))
            }

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

            val attachmentPanel: @Composable () -> Unit = {
                AttachmentPanel(
                    uploads = uploads,
                    artifactState = artifactState,
                    uploadError = attachmentError,
                    onPick = pickAttachment,
                    onRetryUpload = appViewModel::retryUpload,
                    onCancelUpload = appViewModel::cancelUpload,
                    onRefreshArtifacts = appViewModel::refreshArtifacts,
                    onDownloadArtifact = appViewModel::downloadArtifact,
                    onOpenArtifact = appViewModel::openArtifact,
                )
            }

            HermesTalkTheme {
                AppRoot(
                    uiState = uiState,
                    callState = callState,
                    screen = screen,
                    onNavigate = { screen = it },
                    viewModel = appViewModel,
                    attachmentPanel = attachmentPanel,
                    requestConnect = requestConnect,
                    requestCallMicPermission = requestCallMicPermission,
                    requestBatteryExemption = requestBatteryExemption,
                    setWakeWordEnabled = setWakeWordEnabled,
                )
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleShareIntent(intent)
    }

    private fun handleShareIntent(intent: Intent?) {
        if (intent == null) {
            return
        }
        if (
            intent.action == Intent.ACTION_SEND ||
            intent.action == Intent.ACTION_SEND_MULTIPLE
        ) {
            pendingShare.value = intent
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
    attachmentPanel: @Composable () -> Unit,
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
            extraContent = attachmentPanel,
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
            onPushToTalkReleased = {
                viewModel.pushToTalkReleased()
                onNavigate(AppScreen.Call)
            },
        )

        AppScreen.Call -> CallScreen(
            uiState = callState,
            attachmentPanel = attachmentPanel,
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
