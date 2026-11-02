package com.example.hermestalk.wakeword

import android.content.Context
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.io.File

/**
 * Orchestrates the on-device wake word: engine creation, AudioRecord capture,
 * the pure lifecycle machine, the cooldown gate, the false-trigger log and the
 * usage recorder. It never creates or restarts a foreground service.
 */
class WakeWordController(
    initialConfig: WakeWordConfig = WakeWordConfig(),
    private val engineFactory: (WakeWordConfig) -> WakeWordKeywordEngine,
    private val captureFactory: (WakeWordKeywordEngine, WakeWordConfig) -> WakeWordCapture,
    private val permissionChecker: WakeWordPermissionChecker,
    private val falseTriggerLog: FalseTriggerLog,
    private val usage: WakeWordUsage,
    private val onWakeWord: () -> Unit = {},
    private val scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.Default),
    private val clock: () -> Long = System::currentTimeMillis,
) {

    @Volatile
    private var config = initialConfig

    private val stateLock = Any()
    private val _state = MutableStateFlow<WakeWordState>(WakeWordState.Off)

    /** Observable wake-word lifecycle state. */
    val state: StateFlow<WakeWordState> = _state.asStateFlow()

    private val _detections = MutableSharedFlow<FalseTriggerEntry>(extraBufferCapacity = 32)

    /** Accepted wake-word and push-to-talk triggers. */
    val detections: SharedFlow<FalseTriggerEntry> = _detections.asSharedFlow()

    private val _level = MutableStateFlow(0f)

    /** Latest capture RMS level in [0,1]. */
    val audioLevel: StateFlow<Float> = _level.asStateFlow()

    private val pushToTalk = PushToTalkController()

    /** Observable push-to-talk state. */
    val pushToTalkState: StateFlow<PushToTalkState> = pushToTalk.state

    @Volatile
    private var gate = DetectionGate(initialConfig.cooldownMillis)

    @Volatile
    private var engine: WakeWordKeywordEngine? = null

    @Volatile
    private var capture: WakeWordCapture? = null

    @Volatile
    private var lastCapture: WakeWordCapture? = null

    @Volatile
    private var listeningStartedAt: Long? = null

    private var cooldownJob: Job? = null

    @Volatile
    private var released = false

    /** Start the engine and capture. Returns true when listening or already active. */
    fun start(): Boolean {
        if (released) {
            return false
        }
        if (_state.value.isActive) {
            return true
        }
        apply(WakeWordEvent.StartRequested)

        if (!permissionChecker.hasPermission()) {
            apply(WakeWordEvent.PermissionDenied)
            return false
        }

        val currentConfig = config
        val newEngine = try {
            engineFactory(currentConfig)
        } catch (error: WakeWordEngineException) {
            apply(WakeWordEvent.EngineUnavailable(reason(error)))
            return false
        } catch (error: Exception) {
            apply(WakeWordEvent.EngineUnavailable(reason(error)))
            return false
        }
        engine = newEngine
        apply(WakeWordEvent.EngineReady)

        val newCapture = try {
            captureFactory(newEngine, currentConfig)
        } catch (error: Exception) {
            apply(WakeWordEvent.CaptureFailed(reason(error)))
            releaseEngine()
            return false
        }
        capture = newCapture
        lastCapture = newCapture
        newCapture.onKeyword = { keyword -> handleKeyword(keyword) }
        newCapture.onLevel = { level -> _level.value = level }
        newCapture.onFailure = { reason -> handleCaptureFailure(reason) }

        val startResult = try {
            newCapture.start()
        } catch (error: Exception) {
            WakeWordCaptureStart.Failed(reason(error))
        }
        return when (startResult) {
            is WakeWordCaptureStart.Started -> {
                if (_state.value !is WakeWordState.Starting) {
                    // A failure callback already moved the machine on.
                    releaseCapture()
                    releaseEngine()
                    false
                } else {
                    apply(WakeWordEvent.CaptureStarted)
                    usage.startListening()
                    listeningStartedAt = clock()
                    true
                }
            }

            is WakeWordCaptureStart.PermissionDenied -> {
                apply(WakeWordEvent.PermissionDenied)
                releaseCapture()
                releaseEngine()
                false
            }

            is WakeWordCaptureStart.Failed -> {
                handleCaptureFailure(startResult.reason)
                false
            }
        }
    }

    /** Stop capture and release the engine. Safe to call repeatedly. */
    fun stop() {
        if (released) {
            return
        }
        cooldownJob?.cancel()
        cooldownJob = null
        apply(WakeWordEvent.StopRequested)
        releaseCapture()
        releaseEngine()
        gate.reset()
        usage.stopListening()
        listeningStartedAt = null
        _level.value = 0f
        apply(WakeWordEvent.Stopped)
    }

    /** Release everything and cancel the controller scope. */
    fun release() {
        if (released) {
            return
        }
        stop()
        released = true
        scope.cancel()
    }

    /** Apply a new configuration, restarting capture only when it was active. */
    fun updateConfig(newConfig: WakeWordConfig) {
        if (newConfig == config) {
            return
        }
        val wasActive = _state.value.isActive
        if (wasActive) {
            stop()
        }
        config = newConfig
        gate = DetectionGate(newConfig.cooldownMillis)
        falseTriggerLog.updateCap(newConfig.falseTriggerLogCapBytes)
        if (wasActive && !released) {
            start()
        }
    }

    /** Record a push-to-talk press. */
    fun onPushToTalkPressed(): PushToTalkTransition = pushToTalk.pressStarted()

    /** Record a push-to-talk release and emit the session request when pressed. */
    fun onPushToTalkReleased(): PushToTalkTransition {
        val transition = pushToTalk.pressEnded()
        if (transition.effects.contains(PushToTalkEffect.StartSessionRequested)) {
            val entry = FalseTriggerEntry(
                timestampMillis = clock(),
                keyword = config.phrase,
                source = WakeWordSource.PushToTalk,
                threshold = config.threshold,
                listeningMillis = currentListeningMillis(),
            )
            falseTriggerLog.append(entry)
            _detections.tryEmit(entry)
        }
        return transition
    }

    /** Report a push-to-talk capture failure. */
    fun onPushToTalkCaptureFailed(reason: String): PushToTalkTransition =
        pushToTalk.captureFailed(reason)

    /** Report a push-to-talk permission denial. */
    fun onPushToTalkPermissionDenied(): PushToTalkTransition = pushToTalk.permissionDenied()

    /** True while the capture thread is alive. */
    fun captureThreadAlive(): Boolean = (capture ?: lastCapture)?.threadAlive == true

    /** True when the AudioRecord has been released (or was never created). */
    fun audioRecordReleased(): Boolean =
        (capture ?: lastCapture)?.audioRecordReleased ?: true

    /** True when an AudioRecord has been created by this controller. */
    fun audioRecordCreated(): Boolean =
        (capture ?: lastCapture)?.audioRecordCreated == true

    /** Current battery and detection telemetry. */
    fun usageSnapshot(): WakeWordUsageSnapshot = usage.snapshot()

    /** Write `usage.json` in [directory] and return the snapshot. */
    fun writeUsageSummary(directory: File): WakeWordUsageSnapshot =
        usage.writeSummary(directory)

    private fun handleKeyword(keyword: String) {
        val now = clock()
        if (!gate.shouldAccept(now)) {
            return
        }
        gate.record(now)
        apply(WakeWordEvent.KeywordDetected)
        val entry = FalseTriggerEntry(
            timestampMillis = now,
            keyword = config.phrase,
            source = WakeWordSource.WakeWord,
            threshold = config.threshold,
            listeningMillis = currentListeningMillis(now),
        )
        falseTriggerLog.append(entry)
        usage.recordDetection()
        _detections.tryEmit(entry)
        try {
            onWakeWord()
        } catch (_: Exception) {
            // A UI callback must never break the capture loop.
        }
        cooldownJob?.cancel()
        cooldownJob = scope.launch {
            delay(config.cooldownMillis)
            apply(WakeWordEvent.CooldownElapsed)
        }
    }

    private fun handleCaptureFailure(reason: String) {
        when (_state.value) {
            is WakeWordState.Starting,
            is WakeWordState.Listening,
            is WakeWordState.Detected,
            -> Unit

            else -> return
        }
        cooldownJob?.cancel()
        cooldownJob = null
        apply(WakeWordEvent.CaptureFailed(reason))
        releaseCapture()
        releaseEngine()
        usage.stopListening()
        listeningStartedAt = null
        _level.value = 0f
    }

    private fun apply(event: WakeWordEvent) {
        synchronized(stateLock) {
            val transition = WakeWordMachine.reduce(_state.value, event)
            if (transition.state == _state.value && transition.effects.isEmpty()) {
                return
            }
            _state.value = transition.state
        }
    }

    private fun releaseCapture() {
        val current = capture
        capture = null
        if (current != null) {
            try {
                current.stop()
            } catch (_: Exception) {
                // Releasing the microphone must never fail the caller.
            }
        }
    }

    private fun releaseEngine() {
        val current = engine
        engine = null
        if (current != null) {
            try {
                current.release()
            } catch (_: Exception) {
                // Idempotent native release.
            }
        }
    }

    private fun currentListeningMillis(now: Long = clock()): Long =
        listeningStartedAt?.let { WakeWordUsageMath.listeningMillis(it, now) } ?: 0L

    private fun reason(error: Throwable): String =
        error.message?.takeIf { it.isNotBlank() } ?: error.javaClass.simpleName

    companion object {
        /** Build the production controller with the app assets and private files. */
        fun create(
            context: Context,
            onWakeWord: () -> Unit,
            config: WakeWordConfig = WakeWordConfig(),
        ): WakeWordController {
            val logDirectory = File(context.filesDir, "wakeword")
            val log = FalseTriggerLog(logDirectory, config.falseTriggerLogCapBytes)
            return WakeWordController(
                initialConfig = config,
                engineFactory = { engineConfig -> WakeWordEngine.create(context, engineConfig) },
                captureFactory = { engine, captureConfig ->
                    WakeWordListener(
                        engine = engine,
                        permissionChecker = WakeWordListener.permissionChecker(context),
                        config = captureConfig,
                    )
                },
                permissionChecker = WakeWordListener.permissionChecker(context),
                falseTriggerLog = log,
                usage = WakeWordUsage(),
                onWakeWord = onWakeWord,
            )
        }
    }
}
