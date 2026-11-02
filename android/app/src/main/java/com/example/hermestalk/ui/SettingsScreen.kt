package com.example.hermestalk.ui

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
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Visibility
import androidx.compose.material.icons.filled.VisibilityOff
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import com.example.hermestalk.AppUiState
import com.example.hermestalk.FingerprintState
import com.example.hermestalk.R
import com.example.hermestalk.rtvi.ConnectionState
import com.example.hermestalk.rtvi.FailureReason
import com.example.hermestalk.rtvi.Field
import com.example.hermestalk.rtvi.RelayEndpoint
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Settings form for the relay URL, device token and TLS trust decision.
 * Validation errors are shown inline, fingerprint discovery is explicit, and
 * the cleartext-loopback notice is visible before the user connects.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(
    uiState: AppUiState,
    onSave: (String, String, String, Boolean) -> Boolean,
    onConnect: (String, String, String, Boolean) -> Unit,
    onFetchFingerprint: (String) -> Unit,
    onToggleTokenVisibility: () -> Unit,
) {
    var relayUrl by rememberSaveable(uiState.relayUrl) { mutableStateOf(uiState.relayUrl) }
    var token by rememberSaveable(uiState.deviceToken) { mutableStateOf(uiState.deviceToken) }
    var tlsPin by rememberSaveable(uiState.tlsPin) { mutableStateOf(uiState.tlsPin) }
    var trustPlatformCa by rememberSaveable(uiState.trustPlatformCa) {
        mutableStateOf(uiState.trustPlatformCa)
    }

    val urlErrors = uiState.validationErrors.filter { it.field == Field.RelayUrl }
    val tokenErrors = uiState.validationErrors.filter { it.field == Field.DeviceToken }
    val pinErrors = uiState.validationErrors.filter { it.field == Field.TlsPin }
    val endpoint = (RelayEndpoint.parse(relayUrl) as? RelayEndpoint.ParseResult.Valid)?.endpoint
    val insecureLoopback = endpoint?.isCleartext == true && endpoint.isLoopback
    val fingerprintState = uiState.fingerprintState
    val failed = uiState.connectionState as? ConnectionState.Failed
    val formatter = remember { SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.getDefault()) }

    Scaffold(
        topBar = {
            TopAppBar(title = { Text(stringResource(R.string.app_name)) })
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
            Text(
                text = stringResource(R.string.settings_screen_title),
                style = MaterialTheme.typography.headlineSmall,
            )

            OutlinedTextField(
                value = relayUrl,
                onValueChange = { relayUrl = it },
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag("settings_relay_url"),
                label = { Text(stringResource(R.string.relay_url_label)) },
                supportingText = {
                    Text(
                        text = urlErrors.firstOrNull()?.message
                            ?: stringResource(R.string.relay_url_example),
                    )
                },
                isError = urlErrors.isNotEmpty(),
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
            )

            OutlinedTextField(
                value = token,
                onValueChange = { token = it },
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag("settings_token"),
                label = { Text(stringResource(R.string.device_token_label)) },
                supportingText = {
                    Text(
                        text = tokenErrors.firstOrNull()?.message
                            ?: stringResource(R.string.device_token_hint),
                    )
                },
                isError = tokenErrors.isNotEmpty(),
                singleLine = true,
                visualTransformation = if (uiState.tokenVisible) {
                    VisualTransformation.None
                } else {
                    PasswordVisualTransformation()
                },
                trailingIcon = {
                    IconButton(onClick = onToggleTokenVisibility) {
                        Icon(
                            imageVector = if (uiState.tokenVisible) {
                                Icons.Filled.VisibilityOff
                            } else {
                                Icons.Filled.Visibility
                            },
                            contentDescription = stringResource(
                                if (uiState.tokenVisible) R.string.hide_token else R.string.show_token,
                            ),
                        )
                    }
                },
            )

            OutlinedTextField(
                value = tlsPin,
                onValueChange = { tlsPin = it },
                modifier = Modifier.fillMaxWidth(),
                label = { Text(stringResource(R.string.tls_pin_label)) },
                supportingText = {
                    Text(
                        text = pinErrors.firstOrNull()?.message
                            ?: stringResource(R.string.tls_pin_hint),
                    )
                },
                isError = pinErrors.isNotEmpty(),
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Text),
            )

            Button(
                onClick = { onFetchFingerprint(relayUrl) },
                modifier = Modifier.fillMaxWidth(),
                enabled = fingerprintState !is FingerprintState.Fetching,
            ) {
                Text(stringResource(R.string.fetch_fingerprint))
            }

            when (fingerprintState) {
                FingerprintState.Idle -> Unit

                FingerprintState.Fetching -> {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                    ) {
                        CircularProgressIndicator()
                        Text(stringResource(R.string.fingerprint_fetching))
                    }
                }

                is FingerprintState.Found -> {
                    val found = fingerprintState.discovery
                    Card(
                        colors = CardDefaults.cardColors(
                            containerColor = MaterialTheme.colorScheme.secondaryContainer,
                        ),
                    ) {
                        Column(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(16.dp),
                            verticalArrangement = Arrangement.spacedBy(6.dp),
                        ) {
                            Text(
                                text = stringResource(R.string.fingerprint_found_title),
                                style = MaterialTheme.typography.titleMedium,
                                color = MaterialTheme.colorScheme.onSecondaryContainer,
                            )
                            Text(
                                text = stringResource(R.string.spki_pin_value, found.spkiPin),
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSecondaryContainer,
                            )
                            Text(
                                text = stringResource(
                                    R.string.certificate_fingerprint_value,
                                    found.certificateSha256,
                                ),
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSecondaryContainer,
                            )
                            Text(
                                text = stringResource(R.string.fingerprint_subject_value, found.subject),
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSecondaryContainer,
                            )
                            Text(
                                text = stringResource(R.string.fingerprint_issuer_value, found.issuer),
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSecondaryContainer,
                            )
                            Text(
                                text = stringResource(
                                    R.string.fingerprint_expiry_value,
                                    formatter.format(Date(found.notAfterEpochMs)),
                                ),
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSecondaryContainer,
                            )
                            if (found.expired) {
                                Text(
                                    text = stringResource(R.string.fingerprint_expired),
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.error,
                                )
                            }
                            Button(onClick = { tlsPin = found.spkiPin }) {
                                Text(stringResource(R.string.use_fingerprint))
                            }
                        }
                    }
                }

                is FingerprintState.Error -> {
                    Text(
                        text = fingerprintState.message,
                        color = MaterialTheme.colorScheme.error,
                        style = MaterialTheme.typography.bodyMedium,
                    )
                }
            }

            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                Column(modifier = Modifier.weight(1f)) {
                    Text(
                        text = stringResource(R.string.trust_platform_ca),
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    Text(
                        text = stringResource(R.string.trust_platform_ca_hint),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                Switch(
                    checked = trustPlatformCa,
                    onCheckedChange = { trustPlatformCa = it },
                )
            }

            if (insecureLoopback) {
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
                            text = stringResource(R.string.cleartext_notice),
                            color = MaterialTheme.colorScheme.onErrorContainer,
                        )
                    }
                }
            }

            if (failed?.reason == FailureReason.TlsRejected) {
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
                            text = stringResource(R.string.tls_no_retry_hint),
                            color = MaterialTheme.colorScheme.onErrorContainer,
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            }

            if (uiState.tokenSaved) {
                Text(
                    text = stringResource(R.string.token_saved),
                    color = MaterialTheme.colorScheme.primary,
                    style = MaterialTheme.typography.bodyMedium,
                )
            }

            Spacer(Modifier.height(4.dp))

            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Button(
                    onClick = { onSave(relayUrl, token, tlsPin, trustPlatformCa) },
                    modifier = Modifier.weight(1f),
                ) {
                    Text(stringResource(R.string.save))
                }
                Button(
                    onClick = { onConnect(relayUrl, token, tlsPin, trustPlatformCa) },
                    modifier = Modifier
                        .weight(1f)
                        .testTag("settings_connect"),
                ) {
                    Text(stringResource(R.string.connect))
                }
            }
        }
    }
}
