package com.example.hermestalk.call

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.provider.Settings
import androidx.core.content.ContextCompat
import com.example.hermestalk.rtvi.ConnectionState
import com.example.hermestalk.rtvi.RelaySettings
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * Orchestrates the call feature: permission gating, the foreground service, the
 * RTVI session, audio focus, route changes, the level meter and the timer.
 *
 * Every Android interaction goes through an injectable abstraction so the
 * controller can be exercised on the JVM. Public entry points swallow platform
 * exceptions; the UI only observes [uiState].
 */
class CallController(
    private val appContext: Context,
    private val sessionFactory: (CallSessionCallbacks) -> CallSession,
    private val audioFocus: AudioFocusController,
    private val routes: AudioRouteController,
    private val micMonitor: MicActivityMonitor,
    private val foreground: CallForegroundController,
    private val battery: BatteryOptimisation,
    private val scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate),
    private val clock: () -> Long = { System.currentTimeMillis() },
) {

    private val _uiState = MutableStateFlow(CallUiState())

    /** Observable call UI state. */
    val uiState: StateFlow<CallUiState> = _uiState.asStateFlow()

    /** Monotonic heartbeat advanced by the one-second timer. */
    val heartbeat: Long
        get() = heartbeatCounter

    @Volatile
    private var heartbeatCounter = 0L

    private val meter = CallLevelMeter()
    private var session: CallSession? = null
    private var pendingSettings: RelaySettings? = null
    private var timerJob: Job? = null
    private var callStartedAt = 0L
    private var lastLevel = 0f
    private var latestMicActive = false
    private var monitoringMic = false
    private var monitoringRoutes = false
    private var released = false

    private val callbacks = object : CallSessionCallbacks {
        override fun onConnectionState(state: ConnectionState) {
            applyEvent(CallEvent.SessionStateChanged(state))
        }

        override fun onUserAudioLevel(level: Float) {
            lastLevel = if (level.isNaN() || level.isInfinite()) {
                0f
            } else {
                level.coerceIn(0f, 1f)
            }
        }

        override fun onBotTranscript(text: String) {
            log("bot transcript: $text")
        }

        override fun onLog(message: String) {
            log(message)
        }
    }

    init {
        scope.launch {
            audioFocus.outcome.collect { outcome ->
                applyEvent(CallEvent.AudioFocusChanged(outcome))
            }
        }
    }

    /** Start a call with [settings]. Requests the permission when it is missing. */
    fun startCall(settings: RelaySettings) {
        safely {
            if (released) {
                return
            }
            pendingSettings = settings
            callStartedAt = 0L
            lastLevel = 0f
            latestMicActive = false
            meter.clear()
            applyEvent(CallEvent.StartRequested)
            if (
                _uiState.value.state is CallState.AwaitingMicPermission &&
                hasMicPermission()
            ) {
                applyEvent(CallEvent.MicPermissionGranted)
            }
        }
    }

    /** Feed the microphone runtime-permission result back into the call. */
    fun onMicPermissionResult(granted: Boolean) {
        safely {
            applyEvent(
                if (granted) {
                    CallEvent.MicPermissionGranted
                } else {
                    CallEvent.MicPermissionDenied
                },
            )
        }
    }

    /** Toggle the user's microphone preference. */
    fun toggleMute() {
        safely {
            val current = _uiState.value
            if (!current.state.isLive) {
                return
            }
            applyEvent(
                if (current.controls.muted) {
                    CallEvent.UnmuteRequested
                } else {
                    CallEvent.MuteRequested
                },
            )
        }
    }

    /** End the call and complete cleanup. Safe to call repeatedly. */
    fun endCall() {
        safely {
            if (!_uiState.value.state.isLive) {
                return
            }
            applyEvent(CallEvent.EndRequested)
            if (_uiState.value.state is CallState.Ending) {
                applyEvent(CallEvent.EndCompleted)
            }
        }
    }

    /** Select an audio route when it is currently available. */
    fun selectRoute(route: AudioRoute) {
        safely {
            applyEvent(CallEvent.RouteSelected(route))
        }
    }

    /** Re-read the platform audio devices and let the machine choose a route. */
    fun onAudioDevicesChanged() {
        safely {
            applyEvent(CallEvent.AudioDevicesChanged(routes.availableRoutes()))
        }
    }

    /** Cancel work, stop monitoring and release focus and the session. */
    fun release() {
        if (released) {
            return
        }
        released = true
        safely { stopTimer() }
        safely { stopMicMonitoring() }
        safely { stopRouteMonitoring() }
        safely { audioFocus.abandon() }
        safely {
            session?.stop()
            session = null
        }
        safely { foreground.stop() }
        scope.cancel()
    }

    /** Intent that opens the battery-optimisation exemption screen. */
    fun batteryOptimisationIntent(): Intent {
        return try {
            battery.requestIntent()
        } catch (_: Exception) {
            Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)
        }
    }

    private fun applyEvent(event: CallEvent) {
        if (released) {
            return
        }
        val current = _uiState.value
        val transition = CallMachine.reduce(current, event)
        if (
            transition.state == current.state &&
            transition.controls == current.controls &&
            transition.effects.isEmpty()
        ) {
            return
        }

        val wasActive = current.state is CallState.Active
        _uiState.value = CallUiState(transition.state, transition.controls)
        if (transition.state is CallState.Active && !wasActive && callStartedAt == 0L) {
            callStartedAt = clock()
        }
        for (effect in transition.effects) {
            executeEffect(effect)
        }
        afterTransition(transition.state)
    }

    private fun executeEffect(effect: CallEffect) {
        try {
            when (effect) {
                CallEffect.RequestMicPermission -> Unit

                CallEffect.StartForegroundService -> {
                    try {
                        foreground.start()
                    } catch (error: Exception) {
                        log("foreground start failed: ${error.message}")
                        applyEvent(
                            CallEvent.SessionFailed(
                                CallFailureReason.Unexpected,
                                "Could not start the foreground service",
                            ),
                        )
                        return
                    }
                    try {
                        routes.enterCommunicationMode()
                    } catch (_: Exception) {
                        // The route controller is best effort.
                    }
                    startRouteMonitoring()
                    val state = _uiState.value.state
                    if (state is CallState.Starting && state.phase == StartPhase.ServiceStart) {
                        applyEvent(CallEvent.ServiceStarted)
                    }
                }

                CallEffect.UpdateForegroundNotification ->
                    foreground.update(_uiState.value.controls)

                CallEffect.StopForegroundService -> {
                    foreground.stop()
                    stopMicMonitoring()
                    stopRouteMonitoring()
                }

                CallEffect.ConnectSession -> connectSession()

                CallEffect.DisconnectAndReleaseSession -> {
                    val active = session
                    session = null
                    active?.stop()
                }

                CallEffect.AcquireAudioFocus -> {
                    val outcome = audioFocus.request()
                    applyEvent(CallEvent.AudioFocusChanged(outcome))
                }

                CallEffect.AbandonAudioFocus -> audioFocus.abandon()

                CallEffect.ApplyMicState -> {
                    session?.setMicEnabled(!_uiState.value.controls.muted)
                }

                CallEffect.ApplyAudioRoute -> {
                    routes.select(_uiState.value.controls.route)
                }

                CallEffect.ClearCommunicationDevice -> routes.clearDevice()

                CallEffect.RestoreAudioMode -> routes.exitCommunicationMode()

                CallEffect.PromptBatteryOptimisation -> updateBatteryFlag()

                CallEffect.StartTimer -> startTimer()

                CallEffect.StopTimer -> stopTimer()
            }
        } catch (error: Exception) {
            log("effect $effect failed: ${error.message}")
        }
    }

    private fun connectSession() {
        val settings = pendingSettings ?: return
        if (session != null) {
            return
        }
        scope.launch {
            try {
                val created = sessionFactory(callbacks)
                session = created
                created.connect(settings)
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (error: Exception) {
                applyEvent(
                    CallEvent.SessionFailed(
                        CallFailureReason.Unexpected,
                        (error.message ?: "Session failed").take(200),
                    ),
                )
            }
        }
    }

    private fun afterTransition(state: CallState) {
        if (state is CallState.Active) {
            startMicMonitoring()
            updateBatteryFlag()
        }
    }

    private fun startMicMonitoring() {
        if (monitoringMic) {
            return
        }
        monitoringMic = true
        try {
            micMonitor.start { active -> latestMicActive = active }
        } catch (_: Exception) {
            monitoringMic = false
        }
    }

    private fun stopMicMonitoring() {
        if (!monitoringMic) {
            return
        }
        monitoringMic = false
        latestMicActive = false
        try {
            micMonitor.stop()
        } catch (_: Exception) {
            // Best effort.
        }
    }

    private fun startRouteMonitoring() {
        if (monitoringRoutes) {
            return
        }
        monitoringRoutes = true
        try {
            routes.startMonitoring { available ->
                applyEvent(CallEvent.AudioDevicesChanged(available))
            }
        } catch (_: Exception) {
            monitoringRoutes = false
        }
    }

    private fun stopRouteMonitoring() {
        if (!monitoringRoutes) {
            return
        }
        monitoringRoutes = false
        try {
            routes.stopMonitoring()
        } catch (_: Exception) {
            // Best effort.
        }
    }

    private fun startTimer() {
        if (timerJob?.isActive == true) {
            return
        }
        timerJob = scope.launch {
            while (isActive) {
                delay(TIMER_INTERVAL_MS)
                heartbeatCounter += 1L
                if (_uiState.value.state is CallState.Active) {
                    val elapsed = if (callStartedAt > 0L) {
                        ((clock() - callStartedAt) / 1000L).coerceAtLeast(0L)
                    } else {
                        0L
                    }
                    applyEvent(CallEvent.Tick(elapsed))
                    val levels = meter.push(lastLevel)
                    val micActive = try {
                        micMonitor.isRecording()
                    } catch (_: Exception) {
                        false
                    }
                    val batteryNeeded = try {
                        !battery.isIgnoring()
                    } catch (_: Exception) {
                        false
                    }
                    latestMicActive = micActive
                    _uiState.update { current ->
                        if (current.state is CallState.Active) {
                            current.copy(
                                controls = current.controls.copy(
                                    levels = levels,
                                    micActive = micActive,
                                    batteryOptimisationNeeded = batteryNeeded,
                                ),
                            )
                        } else {
                            current
                        }
                    }
                }
            }
        }
    }

    private fun stopTimer() {
        timerJob?.cancel()
        timerJob = null
    }

    private fun updateBatteryFlag() {
        val needed = try {
            !battery.isIgnoring()
        } catch (_: Exception) {
            false
        }
        _uiState.update { current ->
            current.copy(
                controls = current.controls.copy(batteryOptimisationNeeded = needed),
            )
        }
    }

    private fun hasMicPermission(): Boolean {
        return try {
            ContextCompat.checkSelfPermission(
                appContext,
                Manifest.permission.RECORD_AUDIO,
            ) == PackageManager.PERMISSION_GRANTED
        } catch (_: Exception) {
            false
        }
    }

    private inline fun safely(block: () -> Unit) {
        try {
            block()
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (error: Exception) {
            log("call controller error: ${error.message}")
        }
    }

    private fun log(message: String) {
        try {
            android.util.Log.d(TAG, message)
        } catch (_: Exception) {
            // Logging is not allowed to break the controller.
        }
    }

    private companion object {
        const val TAG = "CallController"
        const val TIMER_INTERVAL_MS = 1000L
    }
}

/**
 * Process-wide [CallController] singleton. Create it on the main thread so the
 * RTVI client, which captures a Looper, can be constructed safely.
 */
object CallControllers {

    @Volatile
    private var instance: CallController? = null

    /** Return the shared controller, creating it on first use. */
    fun get(context: Context): CallController {
        val existing = instance
        if (existing != null) {
            return existing
        }
        return synchronized(this) {
            val current = instance
            if (current != null) {
                current
            } else {
                create(context.applicationContext).also { instance = it }
            }
        }
    }

    private fun create(context: Context): CallController = CallController(
        appContext = context,
        sessionFactory = { callbacks -> RtviCallSession(context, callbacks) },
        audioFocus = AndroidAudioFocusController(context),
        routes = AndroidAudioRouteController(context),
        micMonitor = AndroidMicActivityMonitor(context),
        foreground = AndroidCallForegroundController(context),
        battery = AndroidBatteryOptimisation(context),
    )
}
