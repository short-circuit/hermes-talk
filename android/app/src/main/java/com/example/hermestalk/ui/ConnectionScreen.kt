package com.example.hermestalk.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
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
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.example.hermestalk.AppUiState
import com.example.hermestalk.R
import com.example.hermestalk.rtvi.ConnectionState
import com.example.hermestalk.rtvi.FailureReason
import com.example.hermestalk.rtvi.RelayEndpoint
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
    onConnect: () -> Unit,
    onDisconnect: () -> Unit,
    onRetry: () -> Unit,
    onBack: () -> Unit,
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

            if (state is ConnectionState.Failed) {
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
                                text = statusLabel(entry.state),
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
    FailureReason.Unexpected -> stringResource(R.string.error_reason_unexpected)
}

private fun statusColor(state: ConnectionState): Color = when (state) {
    is ConnectionState.Disconnected -> StatusGrey
    is ConnectionState.Connecting -> StatusAmber
    is ConnectionState.Connected -> StatusGreen
    is ConnectionState.Ready -> StatusGreen
    is ConnectionState.Reconnecting -> StatusAmber
    is ConnectionState.Failed -> StatusRed
}
