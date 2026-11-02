package com.example.hermestalk.call.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CallEnd
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.MicOff
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.example.hermestalk.call.AudioRoute
import com.example.hermestalk.call.CallFailureReason
import com.example.hermestalk.call.CallState
import com.example.hermestalk.call.CallTimer
import com.example.hermestalk.call.CallUiState
import com.example.hermestalk.call.R
import com.example.hermestalk.call.StartPhase
import com.example.hermestalk.call.isLive

/**
 * Stateless in-call screen. All state is supplied by [uiState]; every action is
 * delegated to the caller so the screen can be rendered in tests without a
 * controller or an Android audio stack.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CallScreen(
    uiState: CallUiState,
    attachmentPanel: @Composable () -> Unit = {},
    onToggleMute: () -> Unit,
    onEndCall: () -> Unit,
    onSelectRoute: (AudioRoute) -> Unit,
    onRequestMicPermission: () -> Unit,
    onRequestBatteryExemption: () -> Unit,
    onRedial: () -> Unit,
    onBack: () -> Unit,
) {
    val state = uiState.state
    val controls = uiState.controls
    val live = state.isLive

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text(
            text = stringResource(R.string.call_title),
            style = MaterialTheme.typography.headlineMedium,
            fontWeight = FontWeight.Bold,
        )

        Card(
            colors = CardDefaults.cardColors(
                containerColor = MaterialTheme.colorScheme.surfaceVariant,
            ),
        ) {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(20.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                Text(
                    text = statusHeadline(state),
                    style = MaterialTheme.typography.headlineSmall,
                    fontWeight = FontWeight.Bold,
                )
                if (state is CallState.Reconnecting) {
                    Text(
                        text = stringResource(
                            R.string.call_reconnecting_detail,
                            state.attempt,
                            state.detail,
                        ),
                        style = MaterialTheme.typography.bodyMedium,
                    )
                }
                if (
                    state is CallState.Active ||
                    state is CallState.Ending ||
                    state is CallState.Ended
                ) {
                    Text(
                        text = stringResource(
                            R.string.call_duration,
                            CallTimer.format(controls.durationSeconds),
                        ),
                        style = MaterialTheme.typography.bodyLarge,
                    )
                }
            }
        }

        attachmentPanel()

        if (
            state is CallState.AwaitingMicPermission ||
            (state is CallState.Failed &&
                state.reason == CallFailureReason.MicrophonePermissionDenied)
        ) {
            Card(
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag("call_permission_denied"),
                colors = CardDefaults.cardColors(
                    containerColor = MaterialTheme.colorScheme.errorContainer,
                ),
            ) {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Text(
                        text = stringResource(R.string.call_permission_title),
                        color = MaterialTheme.colorScheme.onErrorContainer,
                        fontWeight = FontWeight.Bold,
                    )
                    Text(
                        text = stringResource(R.string.call_permission_detail),
                        color = MaterialTheme.colorScheme.onErrorContainer,
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    Button(onClick = onRequestMicPermission) {
                        Text(stringResource(R.string.grant_microphone))
                    }
                }
            }
        }

        if (state is CallState.Failed) {
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(
                    containerColor = MaterialTheme.colorScheme.errorContainer,
                ),
            ) {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Text(
                        text = failureReasonLabel(state.reason),
                        color = MaterialTheme.colorScheme.onErrorContainer,
                        fontWeight = FontWeight.Bold,
                    )
                    Text(
                        text = state.detail,
                        color = MaterialTheme.colorScheme.onErrorContainer,
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    Button(onClick = onRedial) {
                        Text(stringResource(R.string.redial))
                    }
                }
            }
        }

        MicrophoneVisualiser(
            levels = controls.levels,
            muted = controls.muted,
        )

        if (controls.availableRoutes.isNotEmpty()) {
            Text(
                text = stringResource(R.string.audio_route),
                style = MaterialTheme.typography.titleMedium,
            )
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .horizontalScroll(rememberScrollState()),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                for (route in controls.availableRoutes) {
                    FilterChip(
                        selected = controls.route == route,
                        onClick = { onSelectRoute(route) },
                        label = { Text(routeLabel(route)) },
                    )
                }
            }
        }

        if (controls.batteryOptimisationNeeded) {
            Card(
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag("call_battery_prompt"),
                colors = CardDefaults.cardColors(
                    containerColor = MaterialTheme.colorScheme.secondaryContainer,
                ),
            ) {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Text(
                        text = stringResource(R.string.battery_prompt_title),
                        color = MaterialTheme.colorScheme.onSecondaryContainer,
                        fontWeight = FontWeight.Bold,
                    )
                    Text(
                        text = stringResource(R.string.battery_prompt_detail),
                        color = MaterialTheme.colorScheme.onSecondaryContainer,
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    Button(onClick = onRequestBatteryExemption) {
                        Text(stringResource(R.string.allow_background))
                    }
                }
            }
        }

        if (live) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(12.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                IconButton(
                    onClick = onToggleMute,
                    modifier = Modifier.testTag("call_mute"),
                ) {
                    Icon(
                        imageVector = if (controls.muted) {
                            Icons.Filled.MicOff
                        } else {
                            Icons.Filled.Mic
                        },
                        contentDescription = if (controls.muted) {
                            stringResource(R.string.unmute)
                        } else {
                            stringResource(R.string.mute)
                        },
                    )
                }
                Button(
                    onClick = onEndCall,
                    modifier = Modifier.testTag("call_end"),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = MaterialTheme.colorScheme.error,
                        contentColor = MaterialTheme.colorScheme.onError,
                    ),
                ) {
                    Icon(
                        imageVector = Icons.Filled.CallEnd,
                        contentDescription = null,
                    )
                    Spacer(Modifier.width(8.dp))
                    Text(stringResource(R.string.end_call))
                }
            }
        }

        TextButton(
            onClick = onBack,
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text(stringResource(R.string.back))
        }
    }
}

@Composable
private fun MicrophoneVisualiser(levels: List<Float>, muted: Boolean) {
    val barColor = if (muted) {
        MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.45f)
    } else {
        MaterialTheme.colorScheme.primary
    }
    val centreColor = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.35f)
    Canvas(
        modifier = Modifier
            .fillMaxWidth()
            .height(120.dp)
            .testTag("call_visualiser"),
    ) {
        val centreY = size.height / 2f
        drawLine(
            color = centreColor,
            start = Offset(0f, centreY),
            end = Offset(size.width, centreY),
            strokeWidth = 1.dp.toPx(),
        )
        if (levels.isEmpty()) {
            return@Canvas
        }
        val slot = size.width / levels.size
        val maxAmplitude = size.height / 2f - 4.dp.toPx()
        levels.forEachIndexed { index, raw ->
            val level = raw.coerceIn(0f, 1f)
            val amplitude = level * maxAmplitude
            if (amplitude <= 0f) {
                return@forEachIndexed
            }
            val x = (index + 0.5f) * slot
            drawLine(
                color = barColor,
                start = Offset(x, centreY - amplitude),
                end = Offset(x, centreY + amplitude),
                strokeWidth = slot * 0.45f,
                cap = StrokeCap.Round,
            )
        }
    }
}

@Composable
private fun statusHeadline(state: CallState): String = when (state) {
    is CallState.Idle -> stringResource(R.string.call_status_idle)
    is CallState.AwaitingMicPermission ->
        stringResource(R.string.call_status_awaiting_permission)

    is CallState.Starting -> when (state.phase) {
        StartPhase.ServiceStart -> stringResource(R.string.call_status_starting_service)
        StartPhase.Connecting -> stringResource(R.string.call_status_connecting)
    }

    is CallState.Active -> stringResource(R.string.call_status_active)
    is CallState.Reconnecting -> stringResource(R.string.call_status_reconnecting)
    is CallState.Ending -> stringResource(R.string.call_status_ending)
    is CallState.Ended -> stringResource(R.string.call_status_ended)
    is CallState.Failed -> stringResource(R.string.call_status_failed)
}

@Composable
private fun failureReasonLabel(reason: CallFailureReason): String = when (reason) {
    CallFailureReason.MicrophonePermissionDenied ->
        stringResource(R.string.failure_microphone_permission_denied)

    CallFailureReason.MicrophoneUnavailable ->
        stringResource(R.string.failure_microphone_unavailable)

    CallFailureReason.RelayUnreachable -> stringResource(R.string.failure_relay_unreachable)
    CallFailureReason.RelayRejected -> stringResource(R.string.failure_relay_rejected)
    CallFailureReason.HandshakeFailed -> stringResource(R.string.failure_handshake_failed)
    CallFailureReason.AudioFocusLost -> stringResource(R.string.failure_audio_focus_lost)
    CallFailureReason.Unexpected -> stringResource(R.string.failure_unexpected)
}

@Composable
private fun routeLabel(route: AudioRoute): String = when (route) {
    AudioRoute.Earpiece -> stringResource(R.string.route_earpiece)
    AudioRoute.Speaker -> stringResource(R.string.route_speaker)
    AudioRoute.WiredHeadset -> stringResource(R.string.route_wired_headset)
    AudioRoute.Bluetooth -> stringResource(R.string.route_bluetooth)
}
