package com.example.hermestalk.rtvi

import ai.pipecat.client.PipecatClientOptions
import ai.pipecat.client.result.RTVIException
import ai.pipecat.client.small_webrtc_transport.PipecatClientSmallWebRTC
import ai.pipecat.client.small_webrtc_transport.SmallWebRTCTransport
import ai.pipecat.client.types.APIRequest
import ai.pipecat.client.types.TransportState
import ai.pipecat.client.types.Value
import android.content.Context
import android.os.Handler
import android.os.Looper
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * Owns the Pipecat SmallWebRTC client and translates transport callbacks into
 * [ConnectionMachine] transitions.
 *
 * Construction and release must happen on a thread with a Looper (the main
 * thread) because the Pipecat client captures the current thread reference.
 * The device token is sent only in the Authorization header and is never
 * included in log messages.
 */
class RtviSession(
    context: Context,
    private val probe: HealthProbe = HttpHealthProbe(),
    private val callbacks: SessionCallbacks = SessionCallbacks.None,
) {

    private val appContext = context.applicationContext
    private val mainHandler = Handler(Looper.getMainLooper())
    private val mainScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val _state = MutableStateFlow<ConnectionState>(ConnectionState.Disconnected)

    /** Current connection state. */
    val state: StateFlow<ConnectionState> = _state.asStateFlow()

    private var client: PipecatClientSmallWebRTC? = null
    private var currentSettings: RelaySettings? = null
    private var retryJob: Job? = null
    private var retryScheduled = false
    private var retryAttempt = 0
    private var micEnabled = true
    private var sessionGeneration = 0
    private val watchdog = SessionWatchdog()
    private var watchdogJob: Job? = null
    private var watchdogEndpoint: RelayEndpoint? = null
    private var watchdogSettings: RelaySettings? = null

    init {
        if (Looper.myLooper() == null) {
            throw IllegalStateException(
                "RtviSession must be constructed on a thread with a Looper (the main thread)",
            )
        }
    }

    /**
     * Validate [settings], probe the relay and start an RTVI session. Returns
     * the resulting state and never throws to the caller. While a connection is
     * already active the call is a no-op and returns the current state.
     */
    suspend fun connect(settings: RelaySettings): ConnectionState {
        val before = _state.value
        if (before.isActive) {
            return before
        }

        currentSettings = settings
        retryAttempt = 0
        cancelRetryInternal()
        val generation = ++sessionGeneration
        return try {
            runConnect(settings, emitConnectRequested = true, generation = generation)
        } catch (cancelled: CancellationException) {
            _state.value
        }
    }

    /**
     * Best-effort disconnect. Safe to call repeatedly and when already
     * disconnected. The state is moved to Disconnected through the reducer.
     */
    fun disconnect() {
        sessionGeneration++
        cancelRetryInternal()
        stopWatchdogInternal()
        watchdogEndpoint = null
        watchdogSettings = null
        retryAttempt = 0
        currentSettings = null
        val activeClient = client
        try {
            activeClient?.disconnect()
        } catch (error: Exception) {
            callbacks.onLog("disconnect failed: ${error.message}")
        }
        applyEvent(ConnectionEvent.DisconnectRequested)
        releaseClientInternal()
    }

    /**
     * Release the Pipecat client and cancel retries. Idempotent and safe when
     * the session was never connected. Must be called on the main thread.
     */
    fun release() {
        sessionGeneration++
        cancelRetryInternal()
        stopWatchdogInternal()
        watchdogEndpoint = null
        watchdogSettings = null
        retryAttempt = 0
        currentSettings = null
        applyEvent(ConnectionEvent.DisconnectRequested)
        releaseClientInternal()
    }

    /**
     * Enable or disable the microphone. The SDK exposes `enableMic`; the flag is
     * also retained so a client created later starts with the requested value.
     */
    fun updateMic(enabled: Boolean) {
        micEnabled = enabled
        val activeClient = client ?: return
        try {
            activeClient.enableMic(enabled)
        } catch (error: Exception) {
            callbacks.onLog("microphone update failed: ${error.message}")
        }
    }

    private suspend fun runConnect(
        settings: RelaySettings,
        emitConnectRequested: Boolean,
        generation: Int,
    ): ConnectionState {
        if (emitConnectRequested) {
            applyEvent(ConnectionEvent.ConnectRequested)
        }

        val validationErrors = RelaySettingsValidator.validate(settings)
        if (validationErrors.isNotEmpty()) {
            applyEvent(
                ConnectionEvent.ProbeFailed(
                    FailureReason.InvalidConfiguration,
                    validationErrors.joinToString("; ") { it.message },
                ),
            )
            return _state.value
        }

        val endpoint = when (val parsed = RelayEndpoint.parse(settings.relayUrl)) {
            is RelayEndpoint.ParseResult.Valid -> parsed.endpoint
            is RelayEndpoint.ParseResult.Invalid -> {
                applyEvent(
                    ConnectionEvent.ProbeFailed(FailureReason.InvalidConfiguration, parsed.reason),
                )
                return _state.value
            }
        }

        applyEvent(ConnectionEvent.ProbeStarted)
        if (generation != sessionGeneration) {
            return _state.value
        }

        callbacks.onLog("probing relay at ${endpoint.baseUrl()}")
        val policy = settings.tlsPolicy()
        when (val result = probe.probe(endpoint, settings.deviceToken, policy)) {
            is ProbeResult.Ok -> Unit
            is ProbeResult.Failure -> {
                if (generation == sessionGeneration) {
                    if (result.reason == FailureReason.TlsRejected) {
                        callbacks.onTlsFailure(result.expectedPin, result.observedPin)
                    }
                    applyEvent(ConnectionEvent.ProbeFailed(result.reason, result.detail))
                }
                return _state.value
            }
        }

        if (generation != sessionGeneration) {
            return _state.value
        }

        watchdogEndpoint = endpoint
        watchdogSettings = settings
        applyEvent(ConnectionEvent.ProbeSucceeded)
        applyEvent(ConnectionEvent.NegotiationStarted)

        val activeClient = ensureClient() ?: run {
            applyEvent(
                ConnectionEvent.TransportFailed(
                    FailureReason.Unexpected,
                    "Could not create the WebRTC client",
                ),
            )
            return _state.value
        }

        callbacks.onLog("negotiating with relay at ${endpoint.baseUrl()}")
        return negotiate(activeClient, endpoint, settings, generation)
    }

    private suspend fun negotiate(
        activeClient: PipecatClientSmallWebRTC,
        endpoint: RelayEndpoint,
        settings: RelaySettings,
        generation: Int,
    ): ConnectionState {
        val request = APIRequest(
            endpoint = endpoint.startUrl(),
            requestData = Value.Object(),
            headers = mapOf("Authorization" to "Bearer ${settings.deviceToken}"),
            timeoutMs = 15000L,
        )

        return try {
            activeClient.startBotAndConnect(request).await()
            if (generation != sessionGeneration) {
                return _state.value
            }
            retryAttempt = 0
            synchronizeFromClientState(activeClient)
            _state.value
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (error: RTVIException) {
            if (generation == sessionGeneration) {
                val mapped = mapRtviError(error.error)
                applyEvent(ConnectionEvent.TransportFailed(mapped.first, mapped.second))
            }
            _state.value
        } catch (error: Exception) {
            if (generation == sessionGeneration) {
                applyEvent(
                    ConnectionEvent.TransportFailed(
                        FailureReason.HandshakeFailed,
                        (error.message ?: "Handshake failed").take(MAX_DETAIL),
                    ),
                )
            }
            _state.value
        }
    }

    private fun synchronizeFromClientState(activeClient: PipecatClientSmallWebRTC) {
        when (activeClient.state) {
            TransportState.Ready -> applyEvent(ConnectionEvent.BotReady)
            TransportState.Connected -> applyEvent(ConnectionEvent.TransportConnected)
            else -> Unit
        }
    }

    private fun ensureClient(): PipecatClientSmallWebRTC? {
        val existing = client
        if (existing != null) {
            val existingState = existing.state
            if (
                existingState == TransportState.Initialized ||
                existingState == TransportState.Disconnected
            ) {
                return existing
            }
            callbacks.onLog("replacing stale client in state $existingState")
            releaseClientInternal()
        }
        return try {
            val transport = SmallWebRTCTransport(appContext)
            val options = PipecatClientOptions(
                callbacks = pipecatCallbacks,
                enableMic = micEnabled,
                enableCam = false,
            )
            PipecatClientSmallWebRTC(transport, options).also { client = it }
        } catch (error: Exception) {
            callbacks.onLog("client creation failed: ${error.message}")
            null
        }
    }

    private val pipecatCallbacks = PipecatEventBridge(::dispatch, callbacks)

    private fun dispatch(event: ConnectionEvent) {
        if (Looper.myLooper() == Looper.getMainLooper()) {
            applyEvent(event)
        } else {
            mainHandler.post { applyEvent(event) }
        }
    }

    private fun applyEvent(event: ConnectionEvent): Transition {
        val current = _state.value
        val transition = ConnectionMachine.reduce(current, event)
        if (transition.state == current && transition.effects.isEmpty()) {
            return transition
        }

        _state.value = transition.state
        callbacks.onStateChanged(transition.state)
        syncWatchdog(transition.state)
        if (transition.state is ConnectionState.Failed) {
            releaseClientInternal()
        }
        for (effect in transition.effects) {
            when (effect) {
                ConnectionEffect.ReleaseClient -> releaseClientInternal()
                ConnectionEffect.CancelRetry -> cancelRetryInternal()
                ConnectionEffect.ScheduleRetry -> scheduleRetryInternal()
            }
        }
        return transition
    }

    private fun scheduleRetryInternal() {
        if (retryScheduled) {
            return
        }
        val settings = currentSettings ?: return
        val nextAttempt = retryAttempt + 1
        if (nextAttempt > RETRY_DELAYS_MS.size) {
            applyEvent(ConnectionEvent.RetryExhausted)
            return
        }

        retryAttempt = nextAttempt
        retryScheduled = true
        val delayMs = RETRY_DELAYS_MS[nextAttempt - 1]
        retryJob = mainScope.launch {
            delay(delayMs)
            retryScheduled = false
            if (currentSettings != settings) {
                return@launch
            }
            val generation = ++sessionGeneration
            applyEvent(ConnectionEvent.RetryScheduled(nextAttempt))
            runConnect(settings, emitConnectRequested = false, generation = generation)
        }
    }

    private fun cancelRetryInternal() {
        retryScheduled = false
        retryJob?.cancel()
        retryJob = null
    }

    private fun syncWatchdog(state: ConnectionState) {
        when (state) {
            is ConnectionState.Connected,
            is ConnectionState.Ready,
            -> startWatchdogInternal()

            else -> stopWatchdogInternal()
        }
    }

    private fun startWatchdogInternal() {
        if (watchdogJob?.isActive == true) {
            return
        }
        val endpoint = watchdogEndpoint ?: return
        val settings = watchdogSettings ?: return
        val policy = settings.tlsPolicy()
        watchdog.reset()
        watchdogJob = mainScope.launch {
            while (true) {
                delay(watchdog.interval)
                val result = try {
                    probe.probe(endpoint, settings.deviceToken, policy)
                } catch (cancelled: CancellationException) {
                    throw cancelled
                } catch (_: Exception) {
                    ProbeResult.Failure(FailureReason.Unreachable, "Liveness probe failed")
                }
                if (result is ProbeResult.Failure && result.reason == FailureReason.TlsRejected) {
                    callbacks.onTlsFailure(result.expectedPin, result.observedPin)
                    val current = _state.value
                    if (current is ConnectionState.Connected || current is ConnectionState.Ready) {
                        callbacks.onLog("relay certificate rejected during liveness probe")
                        applyEvent(
                            ConnectionEvent.TransportFailed(
                                FailureReason.TlsRejected,
                                result.detail,
                            ),
                        )
                    }
                    stopWatchdogInternal()
                    return@launch
                }
                val alive = when (result) {
                    is ProbeResult.Ok -> true
                    is ProbeResult.Failure -> when (result.reason) {
                        FailureReason.Unreachable,
                        FailureReason.NotReady,
                        -> false

                        else -> true
                    }
                }
                if (watchdog.onProbeResult(alive) == SessionWatchdog.Action.DeclareLost) {
                    val current = _state.value
                    if (current is ConnectionState.Connected || current is ConnectionState.Ready) {
                        callbacks.onLog("relay liveness lost after repeated probe failures")
                        applyEvent(ConnectionEvent.TransportDisconnected)
                    }
                    stopWatchdogInternal()
                    return@launch
                }
            }
        }
    }

    private fun stopWatchdogInternal() {
        watchdogJob?.cancel()
        watchdogJob = null
        watchdog.reset()
    }

    private fun releaseClientInternal() {
        val activeClient = client ?: return
        client = null
        try {
            activeClient.release()
        } catch (error: Exception) {
            callbacks.onLog("client release failed: ${error.message}")
        }
    }

    private companion object {
        val RETRY_DELAYS_MS = longArrayOf(2000L, 5000L, 10000L)
    }
}
