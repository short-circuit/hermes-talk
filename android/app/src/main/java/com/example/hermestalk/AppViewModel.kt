package com.example.hermestalk

import android.app.Application
import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.example.hermestalk.attachments.ArtifactUiState
import com.example.hermestalk.attachments.Attachments
import com.example.hermestalk.attachments.UploadItem
import com.example.hermestalk.call.AudioRoute
import com.example.hermestalk.call.CallControllers
import com.example.hermestalk.call.CallUiState
import com.example.hermestalk.call.isLive
import com.example.hermestalk.rtvi.ConnectionState
import com.example.hermestalk.rtvi.EncryptedTokenStore
import com.example.hermestalk.rtvi.FieldError
import com.example.hermestalk.rtvi.RelayEndpoint
import com.example.hermestalk.rtvi.RelaySettings
import com.example.hermestalk.rtvi.RelaySettingsValidator
import com.example.hermestalk.rtvi.RtviSession
import com.example.hermestalk.rtvi.SessionCallbacks
import com.example.hermestalk.rtvi.TlsTrust
import com.example.hermestalk.wakeword.PushToTalkEffect
import com.example.hermestalk.wakeword.SharedPreferencesWakeWordSettingsStore
import com.example.hermestalk.wakeword.WakeWordConfig
import com.example.hermestalk.wakeword.WakeWordController
import com.example.hermestalk.wakeword.WakeWordSensitivity
import com.example.hermestalk.wakeword.WakeWordSettings
import com.example.hermestalk.wakeword.WakeWordSource
import com.example.hermestalk.wakeword.WakeWordState
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** One state transition shown in the connection screen's event log. */
data class ConnectionLogEntry(
    val timestampMillis: Long,
    val state: ConnectionState,
    val note: String? = null,
)

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
    val wakeWordEnabled: Boolean = false,
    val wakeWordSensitivity: WakeWordSensitivity = WakeWordSensitivity.Balanced,
    val wakeWordState: WakeWordState = WakeWordState.Off,
    val lastWakeWord: String? = null,
    val falseTriggerCount: Int = 0,
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
    private val attachmentsController = Attachments.get(application)
    private val wakeWordSettingsStore = SharedPreferencesWakeWordSettingsStore(application)
    private val wakeWordController = WakeWordController.create(
        application,
        onWakeWord = {
            viewModelScope.launch { onWakeWordDetected() }
        },
    )

    /** Observable UI state. */
    val uiState: StateFlow<AppUiState> = _uiState.asStateFlow()

    /** Observable in-call state owned by the process-wide call controller. */
    val callState: StateFlow<CallUiState> = callController.uiState

    /** Observable wake-word lifecycle state. */
    val wakeWordState: StateFlow<WakeWordState> = wakeWordController.state

    /** Observable attachment upload queue. */
    val uploadItems: StateFlow<List<UploadItem>> = attachmentsController.uploads

    /** Observable relay artifact list and download state. */
    val artifactState: StateFlow<ArtifactUiState> = attachmentsController.artifactState

    /** Last attachment ingest/upload error, if any. */
    val attachmentError: StateFlow<String?> = attachmentsController.uploadError

    private var callWasLive = false

    init {
        viewModelScope.launch {
            wakeWordController.state.collect { state ->
                _uiState.update { it.copy(wakeWordState = state) }
            }
        }
        viewModelScope.launch {
            wakeWordController.detections.collect { entry ->
                _uiState.update { current ->
                    current.copy(
                        lastWakeWord = if (entry.source == WakeWordSource.WakeWord) {
                            entry.keyword
                        } else {
                            current.lastWakeWord
                        },
                        falseTriggerCount = current.falseTriggerCount +
                            if (entry.source == WakeWordSource.WakeWord) 1 else 0,
                    )
                }
            }
        }
        viewModelScope.launch {
            callState.collect { state ->
                val live = state.state.isLive
                if (live && !callWasLive) {
                    wakeWordController.stop()
                } else if (!live && callWasLive && _uiState.value.wakeWordEnabled) {
                    wakeWordController.start()
                }
                callWasLive = live
            }
        }

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
            // Resume any upload that was persisted before process death.
            attachmentsController.start(stored ?: relaySettings())
        }

        viewModelScope.launch {
            val settings = withContext(Dispatchers.IO) { wakeWordSettingsStore.load() }
            _uiState.update { current ->
                current.copy(
                    wakeWordEnabled = settings.enabled,
                    wakeWordSensitivity = settings.sensitivity,
                )
            }
            wakeWordController.updateConfig(WakeWordConfig(sensitivity = settings.sensitivity))
            if (settings.enabled && !callState.value.state.isLive) {
                wakeWordController.start()
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
        if (errors.isEmpty()) {
            attachmentsController.updateSettings(relaySettings())
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
        if (callState.value.state.isLive) {
            return
        }
        wakeWordController.stop()
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

    /**
     * Queue every file from an ACTION_SEND / ACTION_SEND_MULTIPLE intent. The
     * settings are re-read on an I/O dispatcher so a share that arrives before
     * the first settings load still uses the saved token.
     */
    fun attachFromIntent(intent: Intent) {
        viewModelScope.launch {
            val stored = withContext(Dispatchers.IO) { repository.load() }
            attachmentsController.attachFromIntent(intent, stored ?: relaySettings())
        }
    }

    /** Queue one document-picker URI. */
    fun pickAttachment(uri: Uri) {
        viewModelScope.launch {
            val stored = withContext(Dispatchers.IO) { repository.load() }
            attachmentsController.pickAttachment(uri, stored ?: relaySettings())
        }
    }

    /** Refresh the relay artifact list with the current settings. */
    fun refreshArtifacts() {
        attachmentsController.refreshArtifacts(relaySettings())
    }

    /** Download and verify one artifact. */
    fun downloadArtifact(id: String) {
        attachmentsController.downloadArtifact(id, relaySettings())
    }

    /** Open a verified artifact in an installed viewer. */
    fun openArtifact(id: String) {
        attachmentsController.openArtifact(id)
    }

    /** Retry a failed or cancelled upload. */
    fun retryUpload(id: String) {
        attachmentsController.retryUpload(id, relaySettings())
    }

    /** Cancel an upload and remove its persisted payload. */
    fun cancelUpload(id: String) {
        attachmentsController.cancelUpload(id)
    }

    private fun relaySettings(): RelaySettings {
        val current = _uiState.value
        return RelaySettings(
            relayUrl = current.relayUrl,
            deviceToken = current.deviceToken,
            tlsPin = current.tlsPin,
            trustPlatformCa = current.trustPlatformCa,
        )
    }

    /** Toggle the wake-word preference. */
    fun toggleWakeWord() {
        setWakeWordEnabled(!_uiState.value.wakeWordEnabled)
    }

    /** Persist and apply the wake-word enabled flag. */
    fun setWakeWordEnabled(enabled: Boolean) {
        wakeWordSettingsStore.save(
            WakeWordSettings(
                enabled = enabled,
                sensitivity = _uiState.value.wakeWordSensitivity,
            ),
        )
        _uiState.update { it.copy(wakeWordEnabled = enabled) }
        if (enabled) {
            if (!callState.value.state.isLive) {
                wakeWordController.start()
            }
        } else {
            wakeWordController.stop()
        }
    }

    /** Persist and apply the keyword sensitivity preset. */
    fun setWakeWordSensitivity(preset: WakeWordSensitivity) {
        wakeWordSettingsStore.save(
            WakeWordSettings(
                enabled = _uiState.value.wakeWordEnabled,
                sensitivity = preset,
            ),
        )
        _uiState.update { it.copy(wakeWordSensitivity = preset) }
        wakeWordController.updateConfig(WakeWordConfig(sensitivity = preset))
    }

    /** Feed the wake-word microphone permission result back into the controller. */
    fun onWakeWordPermissionResult(granted: Boolean) {
        if (granted) {
            if (_uiState.value.wakeWordEnabled) {
                wakeWordController.start()
            }
        } else {
            setWakeWordEnabled(true)
        }
    }

    /** Record a push-to-talk press. */
    fun pushToTalkPressed() {
        wakeWordController.onPushToTalkPressed()
    }

    /** Release push-to-talk and start the call when the controller requests it. */
    fun pushToTalkReleased() {
        val transition = wakeWordController.onPushToTalkReleased()
        if (transition.effects.contains(PushToTalkEffect.StartSessionRequested)) {
            appendConnectionLog("Push to talk")
            startCall()
        }
    }

    private fun onWakeWordDetected() {
        appendConnectionLog("Wake word: ${_uiState.value.lastWakeWord ?: "HEY HERMES"}")
        startCall()
    }

    private fun appendConnectionLog(note: String) {
        val entry = ConnectionLogEntry(
            timestampMillis = System.currentTimeMillis(),
            state = _uiState.value.connectionState,
            note = note,
        )
        _uiState.update { current ->
            current.copy(eventLog = (current.eventLog + entry).takeLast(20))
        }
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
        wakeWordController.release()
        session.release()
        super.onCleared()
    }

    private fun isCleartext(raw: String): Boolean {
        val parsed = RelayEndpoint.parse(raw)
        return (parsed as? RelayEndpoint.ParseResult.Valid)?.endpoint?.isCleartext == true
    }
}
