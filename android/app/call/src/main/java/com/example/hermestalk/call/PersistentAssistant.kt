package com.example.hermestalk.call

import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.util.Log
import androidx.core.content.ContextCompat
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update

/** How the persistent assistant service was entered. */
enum class AssistantStartMode {
    /** The app was visible when the service was started. */
    Foreground,

    /** The service was started from a background context such as BOOT_COMPLETED. */
    Background,
}

/** The foreground service type currently held by the persistent assistant. */
enum class AssistantForegroundType {
    /** No typed foreground service; API < 29 only. */
    None,

    /** The microphone foreground service type was accepted. */
    Microphone,

    /** The special-use foreground service type is used as the API 34 fallback. */
    SpecialUse,
}

/** Observable state shared by the app process, the service and instrumentation tests. */
data class PersistentAssistantState(
    val running: Boolean = false,
    val foregroundType: AssistantForegroundType = AssistantForegroundType.None,
    val listening: Boolean = false,
    val wakeLockHeld: Boolean = false,
    val micRefusal: String? = null,
    val startedAtMillis: Long = 0L,
)

/**
 * Pure policy for the always-on assistant plus the process-wide state that the
 * settings UI and instrumentation tests observe.
 *
 * The policy functions deliberately avoid Android framework types so they can be
 * exercised by JVM unit tests. Resource ids are plain integers.
 */
object PersistentAssistant {

    private val _state = MutableStateFlow(PersistentAssistantState())

    /** Current service state, observable by the app and by tests. */
    val state: StateFlow<PersistentAssistantState> = _state.asStateFlow()

    /**
     * Foreground types to try, in order. A microphone foreground service may
     * only be started while the app is visible on API 34+, so the special-use
     * type is the documented fallback. API < 29 has no typed startForeground.
     */
    fun attemptOrder(hasRecordAudio: Boolean, sdkInt: Int): List<AssistantForegroundType> {
        if (sdkInt < Build.VERSION_CODES.Q) {
            return listOf(AssistantForegroundType.None)
        }
        return if (hasRecordAudio) {
            listOf(AssistantForegroundType.Microphone, AssistantForegroundType.SpecialUse)
        } else {
            listOf(AssistantForegroundType.SpecialUse)
        }
    }

    /** The partial wake lock is held only while the assistant is listening. */
    fun shouldHoldWakeLock(listening: Boolean): Boolean = listening

    /** Notification body for the current listening state. */
    fun notificationText(listening: Boolean): Int = if (listening) {
        R.string.persistent_notification_text_listening
    } else {
        R.string.persistent_notification_text_idle
    }

    /** The service must be restarted by the system after it is killed. */
    fun restartMode(): Int = Service.START_STICKY

    /**
     * True for the exceptions that mean the microphone foreground type was
     * refused. The API 34 class is matched by name so the check is safe on
     * devices older than API 31 where the class does not exist.
     */
    fun isMicRefusal(exception: Throwable): Boolean =
        exception is SecurityException ||
            exception is IllegalStateException ||
            exception.javaClass.name ==
            "android.app.ForegroundServiceStartNotAllowedException"

    /** Record of a refused microphone foreground start, for the Settings hint. */
    fun micRefusalRecord(error: Throwable): String =
        "${error.javaClass.name}: ${error.message}"

    /**
     * Tries [candidates] in order and returns the first one whose [attempt] does
     * not throw. [onFailure] observes every rejected candidate, which lets the
     * service record a microphone refusal before retrying the next type.
     */
    internal fun <T> firstSuccessful(
        candidates: List<T>,
        attempt: (T) -> Unit,
        onFailure: (T, Exception) -> Unit,
    ): T? {
        for (candidate in candidates) {
            try {
                attempt(candidate)
                return candidate
            } catch (error: Exception) {
                onFailure(candidate, error)
            }
        }
        return null
    }

    internal fun update(transform: (PersistentAssistantState) -> PersistentAssistantState) {
        _state.update(transform)
    }

    internal fun reset() {
        _state.value = PersistentAssistantState()
    }
}

/**
 * Best-effort Android entry points for the persistent service. Every call is
 * wrapped so a lifecycle race or a background-start restriction never escapes
 * to the caller.
 */
object PersistentAssistantController {

    private const val TAG = "PersistentAssistant"

    /** Start the service from a visible app context. */
    fun startFromForeground(context: Context) {
        start(context, PersistentAssistantService.ACTION_START)
    }

    /** Ask the running service to stop and remove its notification. */
    fun stop(context: Context) {
        try {
            context.startService(
                Intent(context, PersistentAssistantService::class.java)
                    .setAction(PersistentAssistantService.ACTION_STOP),
            )
        } catch (error: Exception) {
            Log.w(TAG, "Could not stop the persistent assistant service", error)
        }
    }

    /** Re-attempt the microphone foreground type from a visible app. */
    fun promoteToMicrophone(context: Context) {
        start(context, PersistentAssistantService.ACTION_PROMOTE_MIC)
    }

    /** Tell the service whether wake-word capture is running. */
    fun setListening(context: Context, listening: Boolean) {
        val intent = Intent(context, PersistentAssistantService::class.java)
            .setAction(PersistentAssistantService.ACTION_SET_LISTENING)
            .putExtra(PersistentAssistantService.EXTRA_LISTENING, listening)
        try {
            ContextCompat.startForegroundService(context, intent)
        } catch (error: Exception) {
            Log.w(TAG, "Could not update listening=$listening", error)
        }
    }

    private fun start(context: Context, action: String) {
        val intent = Intent(context, PersistentAssistantService::class.java)
            .setAction(action)
        try {
            ContextCompat.startForegroundService(context, intent)
        } catch (error: Exception) {
            Log.w(TAG, "Could not start the persistent assistant service ($action)", error)
        }
    }
}

/** The foreground service type as an int, or 0 for the plain API < 29 start. */
internal fun AssistantForegroundType.toServiceInfoType(): Int = when (this) {
    AssistantForegroundType.None -> 0
    AssistantForegroundType.Microphone -> ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
    AssistantForegroundType.SpecialUse -> ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
}
