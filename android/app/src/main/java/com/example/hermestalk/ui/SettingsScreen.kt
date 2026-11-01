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
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import com.example.hermestalk.AppUiState
import com.example.hermestalk.R
import com.example.hermestalk.rtvi.Field
import com.example.hermestalk.rtvi.RelayEndpoint
import androidx.compose.ui.res.stringResource

/**
 * Settings form for the relay URL and device token. Validation errors are shown
 * inline and the cleartext-HTTP notice is visible before the user connects.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(
    uiState: AppUiState,
    onSave: (String, String) -> Boolean,
    onConnect: (String, String) -> Unit,
    onToggleTokenVisibility: () -> Unit,
) {
    var relayUrl by rememberSaveable(uiState.relayUrl) { mutableStateOf(uiState.relayUrl) }
    var token by rememberSaveable(uiState.deviceToken) { mutableStateOf(uiState.deviceToken) }

    val urlErrors = uiState.validationErrors.filter { it.field == Field.RelayUrl }
    val tokenErrors = uiState.validationErrors.filter { it.field == Field.DeviceToken }
    val parsed = RelayEndpoint.parse(relayUrl)
    val insecure = (parsed as? RelayEndpoint.ParseResult.Valid)?.endpoint?.isCleartext == true

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
                modifier = Modifier.fillMaxWidth(),
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
                modifier = Modifier.fillMaxWidth(),
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

            if (insecure) {
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
                    onClick = { onSave(relayUrl, token) },
                    modifier = Modifier.weight(1f),
                ) {
                    Text(stringResource(R.string.save))
                }
                Button(
                    onClick = { onConnect(relayUrl, token) },
                    modifier = Modifier.weight(1f),
                ) {
                    Text(stringResource(R.string.connect))
                }
            }
        }
    }
}
