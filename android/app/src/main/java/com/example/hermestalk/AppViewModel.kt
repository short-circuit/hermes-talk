package com.example.hermestalk

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.example.hermestalk.rtvi.ConnectionState
import com.example.hermestalk.rtvi.EncryptedTokenStore
import com.example.hermestalk.rtvi.FieldError
import com.example.hermestalk.rtvi.RelayEndpoint
import com.example.hermestalk.rtvi.RelaySettings
import com.example.hermestalk.rtvi.RelaySettingsValidator
import com.example.hermestalk.rtvi.RtviSession
import com.example.hermestalk.rtvi.SessionCallbacks
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** One state transition shown in the connection screen's event log. */
data class ConnectionLogEntry(val timestampMillis: Long, val state: ConnectionState)

/** Everything the Compose UI needs to render. */
data class AppUiState(
    val relayUrl: String = BuildConfig.DEFAULT_RELAY_URL,
    val deviceToken: String = "",
    val tokenVisible: Boolean = false,
    val validationErrors: List<FieldError> = emptyList(),
    val connectionState: ConnectionState = ConnectionState.Disconnected,
    val lastError: String? = null,
    val insecureTransport: Boolean = false,
    val tokenSaved: Boolean = false,
    val isBusy: Boolean = false,
    val eventLog: List<ConnectionLogEntry> = emptyList(),
    val micPermissionDenied: Boolean = false,
    val botTranscript: String = "",
    val userAudioLevel: Float = 0f,
)

/**
 * Owns settings persistence and the RTVI session. The session is created on the
 * main thread, which is required by the Pipecat client, and released when the
 * ViewModel is cleared.
 */
class AppViewModel(application: Application) : AndroidViewModel(application) {

    private val repository = SettingsRepository(EncryptedTokenStore(application))
    private val _uiState = MutableStateFlow(AppUiState())
    private val session = RtviSession(
        application,
        callbacks = object : SessionCallbacks {
            override fun onStateChanged(state: ConnectionState) {
                val entry = ConnectionLogEntry(System.currentTimeMillis(), state)
                _uiState.update { current ->
                    current.copy(
                        connectionState = state,
                        isBusy = state is ConnectionState.Connecting ||
                            state is ConnectionState.Reconnecting,
                        lastError = (state as? ConnectionState.Failed)?.detail,
                        eventLog = (current.eventLog + entry).takeLast(20),
                    )
                }
            }

            override fun onBotTranscript(text: String) {
                _uiState.update { it.copy(botTranscript = text) }
            }

            override fun onUserAudioLevel(level: Float) {
                _uiState.update { it.copy(userAudioLevel = level) }
            }

            override fun onLog(message: String) {
                // Diagnostic only; the visible event log is state transitions.
            }
        },
    )

    /** Observable UI state. */
    val uiState: StateFlow<AppUiState> = _uiState.asStateFlow()

    init {
        viewModelScope.launch {
            val stored = withContext(Dispatchers.IO) { repository.load() }
            if (stored != null) {
                _uiState.update { current ->
                    current.copy(
                        relayUrl = stored.relayUrl,
                        deviceToken = stored.deviceToken,
                        tokenSaved = true,
                        insecureTransport = isCleartext(stored.relayUrl),
                    )
                }
            }
        }
    }

    /**
     * Validate and save the settings. Returns true when the values were
     * persisted and false when inline validation errors were produced.
     */
    fun saveSettings(url: String, token: String): Boolean {
        val errors = repository.save(RelaySettings(url, token))
        _uiState.update { current ->
            current.copy(
                relayUrl = url,
                deviceToken = token,
                validationErrors = errors,
                tokenSaved = errors.isEmpty(),
                insecureTransport = isCleartext(url),
            )
        }
        return errors.isEmpty()
    }

    /** Connect using the current settings. No-op when already active. */
    fun connect() {
        val current = _uiState.value
        val settings = RelaySettings(current.relayUrl, current.deviceToken)
        val errors = RelaySettingsValidator.validate(settings)
        if (errors.isNotEmpty()) {
            _uiState.update {
                it.copy(validationErrors = errors, tokenSaved = false)
            }
            return
        }

        viewModelScope.launch {
            _uiState.update { it.copy(isBusy = true, lastError = null) }
            val result = session.connect(settings)
            _uiState.update {
                it.copy(isBusy = result is ConnectionState.Connecting || result is ConnectionState.Reconnecting)
            }
        }
    }

    /** Disconnect and release the active client. Safe to call repeatedly. */
    fun disconnect() {
        session.disconnect()
        _uiState.update { it.copy(isBusy = false) }
    }

    /** Start a fresh connection attempt after a failure. */
    fun retry() {
        connect()
    }

    /** Toggle masked display of the device token. */
    fun toggleTokenVisibility() {
        _uiState.update { it.copy(tokenVisible = !it.tokenVisible) }
    }

    /** Record that the user denied the microphone permission. */
    fun setMicPermissionDenied(denied: Boolean) {
        _uiState.update { it.copy(micPermissionDenied = denied) }
    }

    override fun onCleared() {
        session.release()
        super.onCleared()
    }

    private fun isCleartext(raw: String): Boolean {
        val parsed = RelayEndpoint.parse(raw)
        return (parsed as? RelayEndpoint.ParseResult.Valid)?.endpoint?.isCleartext == true
    }
}
