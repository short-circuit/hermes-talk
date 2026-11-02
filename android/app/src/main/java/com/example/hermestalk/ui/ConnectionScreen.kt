package com.example.hermestalk.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.example.hermestalk.AppUiState
import com.example.hermestalk.R
import com.example.hermestalk.rtvi.ConnectionState
import com.example.hermestalk.rtvi.FailureReason
import com.example.hermestalk.rtvi.RelayEndpoint
import com.example.hermestalk.wakeword.WakeWordSensitivity
import com.example.hermestalk.wakeword.WakeWordState
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

private val StatusGrey = Color(0xFF6B7280)
private val StatusAmber = Color(0xFFB45309)
private val StatusGreen = Color(0xFF15803D)
private val StatusRed = Color(0xFFB91C1C)

/**
 * Connection status screen. It shows the state, failure details, the target
 * relay host (never the token), controls and the recent state transitions.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ConnectionScreen(
    uiState: AppUiState,
    extraContent: @Composable () -> Unit = {},
    onConnect: () -> Unit,
    onDisconnect: () -> Unit,
    onRetry: () -> Unit,
    onStartCall: () -> Unit,
    onBack: () -> Unit,
    onWakeWordToggle: (Boolean) -> Unit,
    onWakeWordSensitivity: (WakeWordSensitivity) -> Unit,
    onPushToTalkPressed: () -> Unit,
    onPushToTalkReleased: () -> Unit,
) {
    val state = uiState.connectionState
    val statusColor = statusColor(state)
    val formatter = remember { SimpleDateFormat("HH:mm:ss", Locale.getDefault()) }
    val endpoint = (RelayEndpoint.parse(uiState.relayUrl) as? RelayEndpoint.ParseResult.Valid)?.endpoint
    val host = endpoint?.let {
        it.host + (it.port?.let { port -> ":$port" } ?: "")
    } ?: stringResource(R.string.not_configured)

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(stringResource(R.string.connection_screen_title)) },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(
                            imageVector = Icons.Filled.ArrowBack,
                            contentDescription = stringResource(R.string.back),
                        )
                    }
                },
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .padding(padding)
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Card(
                colors = CardDefaults.cardColors(
                    containerColor = statusColor.copy(alpha = 0.14f),
                ),
            ) {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(20.dp),
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Text(
                        text = statusLabel(state),
                        style = MaterialTheme.typography.headlineMedium,
                        color = statusColor,
                        fontWeight = FontWeight.Bold,
                    )
                    Text(
                        text = headline(state),
                        style = MaterialTheme.typography.bodyLarge,
                    )
                    if (uiState.isBusy) {
                        CircularProgressIndicator(color = statusColor)
                    }
                }
            }

            extraContent()

            if (state is ConnectionState.Failed && state.reason == FailureReason.TlsRejected) {
                Card(
                    colors = CardDefaults.cardColors(
                        containerColor = MaterialTheme.colorScheme.errorContainer,
                    ),
                ) {
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(16.dp),
                        verticalArrangement = Arrangement.spacedBy(6.dp),
                    ) {
                        Text(
                            text = stringResource(R.string.tls_rejected_title),
                            color = MaterialTheme.colorScheme.onErrorContainer,
                            style = MaterialTheme.typography.titleMedium,
                            fontWeight = FontWeight.Bold,
                        )
                        Text(
                            text = stringResource(R.string.tls_failure_detail),
                            color = MaterialTheme.colorScheme.onErrorContainer,
                            style = MaterialTheme.typography.bodyMedium,
                        )
                        Text(
                            text = stringResource(
                                R.string.expected_pin_value,
                                uiState.expectedPin ?: stringResource(R.string.not_available),
                            ),
                            color = MaterialTheme.colorScheme.onErrorContainer,
                            style = MaterialTheme.typography.bodySmall,
                        )
                        Text(
                            text = stringResource(
                                R.string.observed_pin_value,
                                uiState.observedPin ?: stringResource(R.string.not_available),
                            ),
                            color = MaterialTheme.colorScheme.onErrorContainer,
                            style = MaterialTheme.typography.bodySmall,
                        )
                        Text(
                            text = state.detail,
                            color = MaterialTheme.colorScheme.onErrorContainer,
                            style = MaterialTheme.typography.bodySmall,
                        )
                        Text(
                            text = stringResource(R.string.tls_no_retry_hint),
                            color = MaterialTheme.colorScheme.onErrorContainer,
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            } else if (state is ConnectionState.Failed) {
                Card(
                    colors = CardDefaults.cardColors(
                        containerColor = MaterialTheme.colorScheme.errorContainer,
                    ),
                ) {
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(16.dp),
                        verticalArrangement = Arrangement.spacedBy(6.dp),
                    ) {
                        Text(
                            text = reasonLabel(state.reason),
                            color = MaterialTheme.colorScheme.onErrorContainer,
                            fontWeight = FontWeight.Bold,
                        )
                        Text(
                            text = state.detail,
                            color = MaterialTheme.colorScheme.onErrorContainer,
                            style = MaterialTheme.typography.bodyMedium,
                        )
                        if (state.retryable) {
                            Text(
                                text = stringResource(R.string.retryable_hint),
                                color = MaterialTheme.colorScheme.onErrorContainer,
                                style = MaterialTheme.typography.bodySmall,
                            )
                        }
                    }
                }
            }

            Text(
                text = stringResource(R.string.relay_host_value, host),
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            if (uiState.micPermissionDenied) {
                Card(
                    colors = CardDefaults.cardColors(
                        containerColor = MaterialTheme.colorScheme.errorContainer,
                    ),
                ) {
                    Row(
                        modifier = Modifier.padding(12.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Icon(
                            imageVector = Icons.Filled.Warning,
                            contentDescription = null,
                            tint = MaterialTheme.colorScheme.onErrorContainer,
                        )
                        Spacer(Modifier.width(8.dp))
                        Text(
                            text = stringResource(R.string.mic_denied_notice),
                            color = MaterialTheme.colorScheme.onErrorContainer,
                        )
                    }
                }
            }

            Card {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(10.dp),
                ) {
                    Text(
                        text = stringResource(R.string.wake_word_title),
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold,
                    )
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text(stringResource(R.string.wake_word_enabled))
                        Switch(
                            checked = uiState.wakeWordEnabled,
                            onCheckedChange = onWakeWordToggle,
                            modifier = Modifier.testTag("wake_word_switch"),
                        )
                    }
                    Text(
                        text = stringResource(R.string.wake_word_sensitivity),
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        WakeWordSensitivity.entries.forEach { preset ->
                            FilterChip(
                                selected = uiState.wakeWordSensitivity == preset,
                                onClick = { onWakeWordSensitivity(preset) },
                                label = { Text(sensitivityLabel(preset)) },
                            )
                        }
                    }
                    Button(
                        onClick = {},
                        modifier = Modifier
                            .fillMaxWidth()
                            .testTag("push_to_talk")
                            .pointerInput(Unit) {
                                // Button installs its own clickable, which consumes
                                // the down event. Observe the raw gesture instead of
                                // detectTapGestures so press/release still reaches
                                // push-to-talk.
                                awaitEachGesture {
                                    val down = awaitFirstDown(requireUnconsumed = false)
                                    onPushToTalkPressed()
                                    var pressed = true
                                    while (pressed) {
                                        val event = awaitPointerEvent()
                                        val change = event.changes.firstOrNull {
                                            it.id == down.id
                                        }
                                        pressed = change?.pressed == true
                                    }
                                    onPushToTalkReleased()
                                }
                            },
                    ) {
                        Text(stringResource(R.string.wake_word_hold_to_talk))
                    }
                    Text(
                        text = wakeWordStatus(uiState.wakeWordState),
                        style = MaterialTheme.typography.bodySmall,
                        color = wakeWordStatusColor(uiState.wakeWordState),
                    )
                }
            }

            Button(
                onClick = onConnect,
                modifier = Modifier.fillMaxWidth(),
                enabled = state is ConnectionState.Disconnected || state is ConnectionState.Failed,
            ) {
                Text(stringResource(R.string.connect))
            }
            Button(
                onClick = onDisconnect,
                modifier = Modifier.fillMaxWidth(),
                enabled = state !is ConnectionState.Disconnected,
            ) {
                Text(stringResource(R.string.disconnect))
            }
            Button(
                onClick = onRetry,
                modifier = Modifier.fillMaxWidth(),
                // A manual retry is always allowed after a failure; `retryable` only
                // gates the automatic backoff, which stops after the retry budget.
                enabled = state is ConnectionState.Failed,
            ) {
                Text(stringResource(R.string.retry))
            }
            Button(
                onClick = onStartCall,
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag("start_call"),
                enabled = state is ConnectionState.Ready,
            ) {
                Text(stringResource(R.string.start_call))
            }

            Text(
                text = stringResource(R.string.event_log_title),
                style = MaterialTheme.typography.titleMedium,
            )
            if (uiState.eventLog.isEmpty()) {
                Text(
                    text = stringResource(R.string.no_events),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            } else {
                Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    for (entry in uiState.eventLog.asReversed()) {
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.spacedBy(12.dp),
                        ) {
                            Text(
                                text = formatter.format(Date(entry.timestampMillis)),
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                            Text(
                                text = entry.note ?: statusLabel(entry.state),
                                style = MaterialTheme.typography.bodySmall,
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun statusLabel(state: ConnectionState): String = when (state) {
    is ConnectionState.Disconnected -> stringResource(R.string.status_disconnected)
    is ConnectionState.Connecting -> stringResource(R.string.status_connecting)
    is ConnectionState.Connected -> stringResource(R.string.status_connected)
    is ConnectionState.Ready -> stringResource(R.string.status_ready)
    is ConnectionState.Reconnecting -> stringResource(R.string.status_reconnecting)
    is ConnectionState.Failed -> stringResource(R.string.status_failed)
}

@Composable
private fun headline(state: ConnectionState): String = when (state) {
    is ConnectionState.Disconnected -> stringResource(R.string.headline_disconnected)
    is ConnectionState.Connecting -> stringResource(R.string.headline_connecting)
    is ConnectionState.Connected -> stringResource(R.string.headline_connected)
    is ConnectionState.Ready -> stringResource(R.string.headline_ready)
    is ConnectionState.Reconnecting -> stringResource(R.string.headline_reconnecting)
    is ConnectionState.Failed -> stringResource(R.string.headline_failed)
}

@Composable
private fun reasonLabel(reason: FailureReason): String = when (reason) {
    FailureReason.Unreachable -> stringResource(R.string.error_reason_unreachable)
    FailureReason.Unauthorized -> stringResource(R.string.error_reason_unauthorized)
    FailureReason.NotReady -> stringResource(R.string.error_reason_not_ready)
    FailureReason.HandshakeFailed -> stringResource(R.string.error_reason_handshake_failed)
    FailureReason.PermissionDenied -> stringResource(R.string.error_reason_permission_denied)
    FailureReason.BackendError -> stringResource(R.string.error_reason_backend_error)
    FailureReason.TransportClosed -> stringResource(R.string.error_reason_transport_closed)
    FailureReason.InvalidConfiguration -> stringResource(R.string.error_reason_invalid_configuration)
    FailureReason.TlsRejected -> stringResource(R.string.error_reason_tls_rejected)
    FailureReason.Unexpected -> stringResource(R.string.error_reason_unexpected)
}

@Composable
private fun sensitivityLabel(preset: WakeWordSensitivity): String = when (preset) {
    WakeWordSensitivity.Sensitive -> stringResource(R.string.wake_word_sensitivity_sensitive)
    WakeWordSensitivity.Balanced -> stringResource(R.string.wake_word_sensitivity_balanced)
    WakeWordSensitivity.Strict -> stringResource(R.string.wake_word_sensitivity_strict)
}

@Composable
private fun wakeWordStatus(state: WakeWordState): String = when (state) {
    is WakeWordState.Off -> stringResource(R.string.wake_word_status_off)
    is WakeWordState.Starting -> stringResource(R.string.wake_word_status_starting)
    is WakeWordState.Listening -> stringResource(R.string.wake_word_status_listening)
    is WakeWordState.Detected -> stringResource(R.string.wake_word_status_detected)
    is WakeWordState.Stopping -> stringResource(R.string.wake_word_status_stopping)
    is WakeWordState.PermissionDenied -> stringResource(R.string.wake_word_status_permission_denied)
    is WakeWordState.Unavailable -> stringResource(R.string.wake_word_status_unavailable, state.reason)
    is WakeWordState.Failed -> stringResource(R.string.wake_word_status_failed, state.reason)
}

private fun wakeWordStatusColor(state: WakeWordState): Color = when (state) {
    is WakeWordState.Listening,
    is WakeWordState.Detected,
    -> StatusGreen

    is WakeWordState.Starting,
    is WakeWordState.Stopping,
    -> StatusAmber

    is WakeWordState.PermissionDenied,
    is WakeWordState.Unavailable,
    is WakeWordState.Failed,
    -> StatusRed

    is WakeWordState.Off -> StatusGrey
}

private fun statusColor(state: ConnectionState): Color = when (state) {
    is ConnectionState.Disconnected -> StatusGrey
    is ConnectionState.Connecting -> StatusAmber
    is ConnectionState.Connected -> StatusGreen
    is ConnectionState.Ready -> StatusGreen
    is ConnectionState.Reconnecting -> StatusAmber
    is ConnectionState.Failed -> StatusRed
}
