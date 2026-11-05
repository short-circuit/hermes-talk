package com.example.hermestalk.call

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.util.Log
import androidx.core.content.ContextCompat
import com.example.hermestalk.wakeword.FalseTriggerLog
import com.example.hermestalk.wakeword.WakeWordCapture
import com.example.hermestalk.wakeword.WakeWordConfig
import com.example.hermestalk.wakeword.WakeWordController
import com.example.hermestalk.wakeword.WakeWordEngine
import com.example.hermestalk.wakeword.WakeWordListener
import com.example.hermestalk.wakeword.WakeWordState
import com.example.hermestalk.wakeword.WakeWordUsage
import com.example.hermestalk.wakeword.WakeWordUsageSnapshot
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.io.File

/**
 * The single owner of wake-word capture inside the persistent assistant
 * service. It evaluates the pure policy, starts and stops the controller,
 * performs the ordered detection cleanup, launches the assistant and schedules
 * a delayed resume.
 *
 * The runner is JVM-testable: no Android type appears in the constructor.
 */
class AssistantWakeWordRunner(
    private val controllerFactory: () -> WakeWordController,
    private val launcher: AssistantLauncher,
    private val audioRestore: AssistantAudioRestore,
    private val clock: () -> Long = System::currentTimeMillis,
    private val onListeningChanged: (Boolean) -> Unit = {},
    private val restartDelayMillis: Long = 8_000L,
    private val scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.Default),
    private val foregroundTypeProvider: () -> AssistantForegroundType = {
        AssistantForegroundType.None
    },
    private val callLiveProvider: () -> Boolean = { false },
    private val hasRecordAudioProvider: () -> Boolean = { true },
    private val enabledProvider: () -> Boolean = { true },
    private val falseTriggerLog: FalseTriggerLog? = null,
) {

    private val controller = controllerFactory()
    private val captureLock = Any()

    @Volatile
    private var released = false

    @Volatile
    private var startInFlight = false

    @Volatile
    private var captureActive = false

    @Volatile
    private var requestedEnabled = false

    @Volatile
    private var suspended = false

    @Volatile
    private var reportedListening = false

    @Volatile
    private var lastFailure: String? = null

    private var resumeJob: Job? = null

    @Volatile
    internal var foregroundType: AssistantForegroundType = AssistantForegroundType.None
        private set

    @Volatile
    internal var callLive = false
        private set

    /** Enable or disable the runner. Disabling stops capture synchronously. */
    fun setEnabled(enabled: Boolean) {
        requestedEnabled = enabled
        AssistantWakeWord.update { it.copy(enabled = enabled) }
        if (enabled) {
            refresh()
        } else {
            stopCaptureAndReport()
        }
    }

    /** Stop capture synchronously before a call starts. */
    fun suspendForCall(): Boolean {
        suspended = true
        resumeJob?.cancel()
        resumeJob = null
        stopControllerCapture()
        publishState(inputs())
        reportListening(false)
        return try {
            controller.audioRecordReleased()
        } catch (_: Exception) {
            false
        }
    }

    /** Allow capture to resume after a call ends. */
    fun resumeAfterCall() {
        suspended = false
        refresh()
    }

    /** Apply a foreground-service type change and re-evaluate the policy. */
    fun onForegroundTypeChanged(type: AssistantForegroundType) {
        foregroundType = type
        AssistantWakeWord.update { it.copy(foregroundType = type) }
        refresh()
    }

    /** Apply a call live/idle change; a live call suspends capture. */
    fun onCallLiveChanged(live: Boolean) {
        callLive = live
        if (live) {
            suspendForCall()
        } else {
            resumeAfterCall()
        }
    }

    /** Re-evaluate the policy and start or stop capture accordingly. */
    fun refresh() {
        if (released) {
            return
        }
        val inputs = inputs()
        val shouldCapture = !suspended && AssistantWakeWordPolicy.shouldCapture(inputs)
        if (shouldCapture) {
            if (!captureActive && !startInFlight) {
                startCapture()
            }
        } else if (captureActive || startInFlight) {
            stopCaptureAndReport()
        }
        publishState(inputs)
    }

    /** Apply a new wake-word configuration to the controller. */
    fun updateConfig(config: WakeWordConfig) {
        try {
            controller.updateConfig(config)
        } catch (error: Exception) {
            Log.w(TAG, "Could not update the wake-word configuration", error)
        }
        refresh()
    }

    /** True when the controller's AudioRecord has been released. */
    fun audioRecordReleased(): Boolean = try {
        controller.audioRecordReleased()
    } catch (_: Exception) {
        true
    }

    /** True while the controller's capture thread is alive. */
    fun captureThreadAlive(): Boolean = try {
        controller.captureThreadAlive()
    } catch (_: Exception) {
        false
    }

    /** Latest battery and detection telemetry. */
    fun usageSnapshot(): WakeWordUsageSnapshot? = usageSnapshotOrNull()

    /** Release the controller, cancel the scope and clear the process handle. */
    fun release() {
        if (released) {
            return
        }
        released = true
        resumeJob?.cancel()
        resumeJob = null
        reportListening(false)
        try {
            controller.release()
        } catch (error: Exception) {
            Log.w(TAG, "Could not release the wake-word controller", error)
        }
        scope.cancel()
        if (AssistantWakeWord.runner === this) {
            AssistantWakeWord.runner = null
        }
        AssistantWakeWord.update {
            it.copy(listening = false, micOwner = AssistantMicOwner.None)
        }
    }

    private fun inputs(): AssistantListeningInputs = AssistantListeningInputs(
        wakeWordEnabled = requestedEnabled && enabledProvider(),
        assistantEnabled = true,
        callLive = callLiveProvider(),
        hasRecordAudio = hasRecordAudioProvider(),
        foregroundType = foregroundTypeProvider(),
    )

    private fun startCapture() {
        startInFlight = true
        scope.launch {
            var started = false
            try {
                synchronized(captureLock) {
                    if (released) {
                        return@launch
                    }
                    started = controller.start()
                    captureActive =
                        started && controller.state.value is WakeWordState.Listening
                    if (!started) {
                        lastFailure = controllerFailure() ?: "Could not start wake-word capture"
                    }
                }
            } catch (error: Exception) {
                lastFailure = reason(error)
                captureActive = false
            } finally {
                startInFlight = false
            }

            if (started && (suspended || !AssistantWakeWordPolicy.shouldCapture(inputs()))) {
                try {
                    synchronized(captureLock) {
                        controller.stop()
                    }
                } catch (error: Exception) {
                    Log.w(TAG, "Could not stop capture after a policy change", error)
                }
                captureActive = false
                started = false
            }
            if (!started) {
                captureActive = false
            }

            publishState(inputs())
            reportListening(captureActive)
            updateUsage()
        }
    }

    private fun stopCaptureAndReport() {
        stopControllerCapture()
        publishState(inputs())
        reportListening(false)
    }

    private fun stopControllerCapture() {
        try {
            synchronized(captureLock) {
                if (captureActive || startInFlight) {
                    controller.stop()
                }
                captureActive = false
            }
        } catch (error: Exception) {
            Log.w(TAG, "Could not stop wake-word capture", error)
            captureActive = false
        }
        updateUsage()
    }

    private fun reportListening(listening: Boolean) {
        if (reportedListening == listening) {
            return
        }
        reportedListening = listening
        try {
            onListeningChanged(listening)
        } catch (error: Exception) {
            Log.w(TAG, "Listening callback failed", error)
        }
    }

    private fun publishState(inputs: AssistantListeningInputs) {
        val snapshot = usageSnapshotOrNull()
        val owner = if (captureActive) {
            AssistantMicOwner.WakeWord
        } else {
            AssistantWakeWordPolicy.micOwner(inputs)
        }
        AssistantWakeWord.update { current ->
            current.copy(
                enabled = requestedEnabled,
                listening = captureActive,
                micOwner = owner,
                foregroundType = foregroundType,
                lastFailure = lastFailure,
                listeningWallMillis = snapshot?.listeningWallMillis
                    ?: current.listeningWallMillis,
                cpuMillis = snapshot?.cpuMillis ?: current.cpuMillis,
            )
        }
    }

    private fun updateUsage() {
        val snapshot = usageSnapshotOrNull() ?: return
        AssistantWakeWord.update {
            it.copy(
                listeningWallMillis = snapshot.listeningWallMillis,
                cpuMillis = snapshot.cpuMillis,
            )
        }
    }

    private fun usageSnapshotOrNull(): WakeWordUsageSnapshot? = try {
        controller.usageSnapshot()
    } catch (_: Exception) {
        null
    }

    private fun controllerFailure(): String? = when (val state = controller.state.value) {
        is WakeWordState.Unavailable -> "Unavailable: ${state.reason}"
        is WakeWordState.Failed -> "Failed: ${state.reason}"
        WakeWordState.PermissionDenied -> "Permission denied"
        else -> null
    }

    /**
     * Called from the controller's capture thread. The actual cleanup runs on
     * [scope] so the capture thread can finish its current chunk and exit while
     * the controller releases the AudioRecord.
     */
    internal fun handleDetection() {
        if (released) {
            return
        }
        scope.launch { runDetection() }
    }

    private fun runDetection() {
        try {
            val detectedAt = clock()
            val falseTriggerCount = try {
                falseTriggerLog?.readRecent(1000)?.size
                    ?: AssistantWakeWord.state.value.falseTriggerCount
            } catch (_: Exception) {
                AssistantWakeWord.state.value.falseTriggerCount
            }
            AssistantWakeWord.update {
                it.copy(
                    detectionCount = it.detectionCount + 1,
                    falseTriggerCount = falseTriggerCount,
                    lastDetectionAtMillis = detectedAt,
                )
            }

            var stepStarted = clock()
            stopControllerCapture()
            recordStep(AssistantDetectionStep.StopCapture, stepStarted)

            stepStarted = clock()
            reportListening(false)
            recordStep(AssistantDetectionStep.ReleaseListeningWakeLock, stepStarted)

            val restoreStarted = clock()
            val restored = try {
                audioRestore.restoreAfterDetection()
            } catch (_: Exception) {
                false
            }
            recordStep(AssistantDetectionStep.AbandonAudioFocus, restoreStarted)
            recordStep(AssistantDetectionStep.ClearCommunicationDevice, restoreStarted)
            recordStep(AssistantDetectionStep.ExitCommunicationMode, restoreStarted)

            val launchStarted = clock()
            val result = try {
                launcher.launchWakeWord()
            } catch (error: Exception) {
                AssistantLaunchResult(started = false, failure = reason(error))
            }
            recordStep(AssistantDetectionStep.LaunchAssistant, launchStarted)

            val latency = clock() - detectedAt
            AssistantWakeWord.update {
                it.copy(
                    listening = false,
                    micOwner = AssistantMicOwner.None,
                    lastLaunchLatencyMillis = latency,
                    lastLaunchWithinBudget =
                        AssistantWakeWordPolicy.isWithinBudget(latency),
                    lastLaunchStarted = result.started,
                    lastFailure = result.failure,
                    audioRestored = restored,
                    foregroundType = foregroundType,
                )
            }
            updateUsage()

            resumeJob?.cancel()
            resumeJob = scope.launch {
                delay(restartDelayMillis)
                refresh()
            }
        } catch (error: Exception) {
            Log.e(TAG, "Wake-word detection failed", error)
        }
    }

    private fun recordStep(step: AssistantDetectionStep, startedAt: Long) {
        val elapsed = (clock() - startedAt).coerceAtLeast(0L)
        Log.i(TAG, "detection step=${step.name} durationMs=$elapsed")
    }

    private fun reason(error: Throwable): String =
        error.message?.takeIf { it.isNotBlank() } ?: error.javaClass.simpleName

    companion object {

        private const val TAG = "AssistantWakeWord"

        /** Build the production runner with the app assets and private files. */
        fun create(
            context: Context,
            onListeningChanged: (Boolean) -> Unit,
        ): AssistantWakeWordRunner {
            val appContext = context.applicationContext
            val config = AssistantWakeWordHooks.configOverride ?: WakeWordConfig()
            val log = FalseTriggerLog(
                File(appContext.filesDir, "wakeword"),
                config.falseTriggerLogCapBytes,
            )

            lateinit var runner: AssistantWakeWordRunner
            val controllerFactory = {
                WakeWordController(
                    initialConfig = config,
                    engineFactory = { engineConfig ->
                        WakeWordEngine.create(appContext, engineConfig)
                    },
                    captureFactory = AssistantWakeWordHooks.captureFactory
                        ?: { engine, captureConfig ->
                            WakeWordListener(
                                engine = engine,
                                permissionChecker =
                                    WakeWordListener.permissionChecker(appContext),
                                config = captureConfig,
                            )
                        },
                    permissionChecker = WakeWordListener.permissionChecker(appContext),
                    falseTriggerLog = log,
                    usage = WakeWordUsage(),
                    onWakeWord = { runner.handleDetection() },
                    scope = CoroutineScope(SupervisorJob() + Dispatchers.Default),
                )
            }

            runner = AssistantWakeWordRunner(
                controllerFactory = controllerFactory,
                launcher = AssistantWakeWordHooks.launcher
                    ?: AndroidAssistantLauncher(appContext),
                audioRestore = AssistantWakeWordHooks.audioRestore
                    ?: AndroidAssistantAudioRestore(appContext),
                onListeningChanged = onListeningChanged,
                foregroundTypeProvider = { runner.foregroundType },
                callLiveProvider = { runner.callLive },
                hasRecordAudioProvider = { hasRecordAudio(appContext) },
                enabledProvider = { true },
                falseTriggerLog = log,
            )
            return runner
        }

        private fun hasRecordAudio(context: Context): Boolean = try {
            ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.RECORD_AUDIO,
            ) == PackageManager.PERMISSION_GRANTED
        } catch (_: Exception) {
            false
        }
    }
}
