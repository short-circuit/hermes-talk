package com.example.hermestalk

import android.app.Application
import android.content.Context
import android.content.Intent
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.example.hermestalk.call.AudioRoute
import com.example.hermestalk.call.CallControllers
import com.example.hermestalk.call.CallUiState
import com.example.hermestalk.rtvi.ConnectionState
import com.example.hermestalk.rtvi.EncryptedTokenStore
import com.example.hermestalk.rtvi.FieldError
import com.example.hermestalk.rtvi.RelayEndpoint
import com.example.hermestalk.rtvi.RelaySettings
import com.example.hermestalk.rtvi.RelaySettingsValidator
import com.example.hermestalk.rtvi.RtviSession
import com.example.hermestalk.rtvi.SessionCallbacks
import com.example.hermestalk.rtvi.TlsTrust
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** One state transition shown in the connection screen's event log. */
data class ConnectionLogEntry(val timestampMillis: Long, val state: ConnectionState)

/** Fingerprint discovery progress shown on the settings screen. */
sealed interface FingerprintState {
    /** No discovery has been requested. */
    data object Idle : FingerprintState

    /** A TLS handshake is in progress. */
    data object Fetching : FingerprintState

    /** The relay certificate was read successfully. */
    data class Found(val discovery: TlsTrust.Discovery) : FingerprintState

    /** Discovery failed; [message] is safe to show. */
    data class Error(val message: String) : FingerprintState
}

/** Everything the Compose UI needs to render. */
data class AppUiState(
    val relayUrl: String = BuildConfig.DEFAULT_RELAY_URL,
    val deviceToken: String = "",
    val tlsPin: String = "",
    val trustPlatformCa: Boolean = false,
    val tokenVisible: Boolean = false,
    val validationErrors: List<FieldError> = emptyList(),
    val connectionState: ConnectionState = ConnectionState.Disconnected,
    val lastError: String? = null,
    val expectedPin: String? = null,
    val observedPin: String? = null,
    val fingerprintState: FingerprintState = FingerprintState.Idle,
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

            override fun onTlsFailure(expectedPin: String?, observedPin: String?) {
                _uiState.update {
                    it.copy(expectedPin = expectedPin, observedPin = observedPin)
                }
            }
        },
    )

    private val callController = CallControllers.get(application)

    /** Observable UI state. */
    val uiState: StateFlow<AppUiState> = _uiState.asStateFlow()

    /** Observable in-call state owned by the process-wide call controller. */
    val callState: StateFlow<CallUiState> = callController.uiState

    init {
        viewModelScope.launch {
            val stored = withContext(Dispatchers.IO) { repository.load() }
            if (stored != null) {
                _uiState.update { current ->
                    current.copy(
                        relayUrl = stored.relayUrl,
                        deviceToken = stored.deviceToken,
                        tlsPin = stored.tlsPin,
                        trustPlatformCa = stored.trustPlatformCa,
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
    fun saveSettings(
        url: String,
        token: String,
        tlsPin: String,
        trustPlatformCa: Boolean,
    ): Boolean {
        val errors = repository.save(
            RelaySettings(
                relayUrl = url,
                deviceToken = token,
                tlsPin = tlsPin,
                trustPlatformCa = trustPlatformCa,
            ),
        )
        _uiState.update { current ->
            current.copy(
                relayUrl = url,
                deviceToken = token,
                tlsPin = tlsPin,
                trustPlatformCa = trustPlatformCa,
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
        val settings = RelaySettings(
            relayUrl = current.relayUrl,
            deviceToken = current.deviceToken,
            tlsPin = current.tlsPin,
            trustPlatformCa = current.trustPlatformCa,
        )
        val errors = RelaySettingsValidator.validate(settings)
        if (errors.isNotEmpty()) {
            _uiState.update {
                it.copy(validationErrors = errors, tokenSaved = false)
            }
            return
        }

        viewModelScope.launch {
            _uiState.update {
                it.copy(
                    isBusy = true,
                    lastError = null,
                    expectedPin = null,
                    observedPin = null,
                )
            }
            val result = session.connect(settings)
            _uiState.update {
                it.copy(isBusy = result is ConnectionState.Connecting || result is ConnectionState.Reconnecting)
            }
        }
    }

    /**
     * Read the relay certificate over TLS and publish its fingerprint. Plain
     * HTTP has no certificate, so a loopback HTTP URL produces an explanatory
     * error instead of a discovery attempt.
     */
    fun fetchFingerprint(url: String) {
        when (val parsed = RelayEndpoint.parse(url)) {
            is RelayEndpoint.ParseResult.Invalid -> {
                _uiState.update {
                    it.copy(fingerprintState = FingerprintState.Error(parsed.reason))
                }
                return
            }

            is RelayEndpoint.ParseResult.Valid -> {
                val endpoint = parsed.endpoint
                if (endpoint.isCleartext) {
                    val message = if (endpoint.isLoopback) {
                        "Plain HTTP on loopback has no certificate; no pin is needed."
                    } else {
                        "Plain HTTP has no certificate; use an https URL."
                    }
                    _uiState.update {
                        it.copy(fingerprintState = FingerprintState.Error(message))
                    }
                    return
                }

                val host = endpoint.host
                val port = endpoint.port ?: 443
                _uiState.update {
                    it.copy(fingerprintState = FingerprintState.Fetching)
                }
                viewModelScope.launch {
                    try {
                        val discovery = withContext(Dispatchers.IO) {
                            TlsTrust.discover(host, port, 5000)
                        }
                        _uiState.update {
                            it.copy(fingerprintState = FingerprintState.Found(discovery))
                        }
                    } catch (cancelled: CancellationException) {
                        throw cancelled
                    } catch (error: Exception) {
                        _uiState.update {
                            it.copy(
                                fingerprintState = FingerprintState.Error(
                                    (error.message ?: "Could not fetch the relay certificate")
                                        .take(200),
                                ),
                            )
                        }
                    }
                }
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

    /**
     * Disconnect the connection-screen session first, then start the call with
     * the stored relay settings.
     */
    fun startCall() {
        session.disconnect()
        _uiState.update { it.copy(isBusy = false) }
        val current = _uiState.value
        callController.startCall(
            RelaySettings(
                relayUrl = current.relayUrl,
                deviceToken = current.deviceToken,
                tlsPin = current.tlsPin,
                trustPlatformCa = current.trustPlatformCa,
            ),
        )
    }

    /** Feed the call microphone permission result into the call controller. */
    fun onCallMicPermissionResult(granted: Boolean) {
        callController.onMicPermissionResult(granted)
    }

    /** End the active call. Safe to call repeatedly. */
    fun endCall() {
        callController.endCall()
    }

    /** Toggle the call microphone mute preference. */
    fun toggleCallMute() {
        callController.toggleMute()
    }

    /** Select an audio route for the active call. */
    fun selectCallRoute(route: AudioRoute) {
        callController.selectRoute(route)
    }

    /** Re-read the platform audio devices for the active call. */
    fun onCallAudioDevicesChanged() {
        callController.onAudioDevicesChanged()
    }

    /** Intent that opens the battery-optimisation exemption screen, if any. */
    fun requestBatteryExemption(context: Context): Intent? {
        return runCatching {
            val intent = callController.batteryOptimisationIntent()
            if (context.packageManager.resolveActivity(intent, 0) == null) {
                null
            } else {
                intent
            }
        }.getOrNull()
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
