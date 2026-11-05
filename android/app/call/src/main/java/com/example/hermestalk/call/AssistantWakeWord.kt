package com.example.hermestalk.call

import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.core.content.ContextCompat
import com.example.hermestalk.wakeword.WakeWordCapture
import com.example.hermestalk.wakeword.WakeWordConfig
import com.example.hermestalk.wakeword.WakeWordKeywordEngine
import com.example.hermestalk.wakeword.WakeWordUsageSnapshot
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update

/** Observable state of the wake-word runner inside the persistent service. */
data class AssistantWakeWordState(
    val enabled: Boolean = false,
    val listening: Boolean = false,
    val micOwner: AssistantMicOwner = AssistantMicOwner.None,
    val detectionCount: Int = 0,
    val falseTriggerCount: Int = 0,
    val lastDetectionAtMillis: Long = 0L,
    val lastLaunchLatencyMillis: Long = 0L,
    val lastLaunchWithinBudget: Boolean = false,
    val lastLaunchStarted: Boolean = false,
    val lastFailure: String? = null,
    val audioRestored: Boolean = false,
    val listeningWallMillis: Long = 0L,
    val cpuMillis: Long = 0L,
    val foregroundType: AssistantForegroundType = AssistantForegroundType.None,
)

/**
 * Instrumentation-only seams for the production wake-word runner. They are
 * null in production and reset between instrumentation tests.
 */
object AssistantWakeWordHooks {
    @Volatile
    var captureFactory: ((WakeWordKeywordEngine, WakeWordConfig) -> WakeWordCapture)? = null

    @Volatile
    var configOverride: WakeWordConfig? = null

    @Volatile
    var launcher: AssistantLauncher? = null

    @Volatile
    var audioRestore: AssistantAudioRestore? = null

    fun reset() {
        captureFactory = null
        configOverride = null
        launcher = null
        audioRestore = null
    }
}

/**
 * Process-wide wake-word state and runner handle. The app UI observes [state];
 * the call controller uses [suspendForCall] and [resumeAfterCall] so a live
 * call always wins the microphone.
 */
object AssistantWakeWord {

    private val _state = MutableStateFlow(AssistantWakeWordState())

    /** Observable wake-word runner state. */
    val state: StateFlow<AssistantWakeWordState> = _state.asStateFlow()

    @Volatile
    internal var runner: AssistantWakeWordRunner? = null

    internal fun update(transform: (AssistantWakeWordState) -> AssistantWakeWordState) {
        _state.update(transform)
    }

    /** Reset the observable state; used by instrumentation tests. */
    fun reset() {
        _state.value = AssistantWakeWordState()
    }

    /** Stop wake-word capture synchronously. Returns true when the microphone is released. */
    fun suspendForCall(): Boolean = runner?.suspendForCall() ?: true

    /** Resume wake-word capture when the policy still allows it. */
    fun resumeAfterCall() {
        runner?.resumeAfterCall()
    }

    /** True when the runner's AudioRecord has been released. */
    fun audioRecordReleased(): Boolean = runner?.audioRecordReleased() ?: true

    /** True while the runner's capture thread is alive. */
    fun captureThreadAlive(): Boolean = runner?.captureThreadAlive() ?: false

    /** Latest battery and detection telemetry from the runner, if any. */
    fun usageSnapshot(): WakeWordUsageSnapshot? = runner?.usageSnapshot()
}

/** Best-effort entry point for enabling or disabling the service runner. */
object AssistantWakeWordController {

    private const val TAG = "AssistantWakeWord"

    fun setEnabled(context: Context, enabled: Boolean) {
        val intent = Intent(context, PersistentAssistantService::class.java)
            .setAction(PersistentAssistantService.ACTION_SET_WAKE_WORD)
            .putExtra(PersistentAssistantService.EXTRA_WAKE_WORD_ENABLED, enabled)
        try {
            ContextCompat.startForegroundService(context, intent)
        } catch (error: Exception) {
            Log.w(TAG, "Could not set wake word enabled=$enabled", error)
        }
    }
}
