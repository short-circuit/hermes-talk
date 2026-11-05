package com.example.hermestalk.call

import android.content.Context
import android.content.Intent
import android.os.SystemClock

/** Extra that marks an ACTION_ASSIST launch as coming from the wake word. */
const val EXTRA_WAKE_WORD = "com.example.hermestalk.extra.WAKE_WORD"

/** Opens the assistant entry point after a wake-word detection. */
interface AssistantLauncher {
    fun launchWakeWord(): AssistantLaunchResult
}

/** Result of an attempted assistant launch. */
data class AssistantLaunchResult(
    val started: Boolean,
    val blocked: Boolean = false,
    val latencyMillis: Long = 0L,
    val failure: String? = null,
)

/**
 * Starts the exported MainActivity with ACTION_ASSIST and the wake-word extra.
 *
 * A foreground service is not a background-activity-launch exemption, so the
 * platform may silently refuse the start when the app is not visible. The
 * result therefore records the measured latency and any exception; the
 * foreground notification's content intent remains the one-tap fallback.
 */
class AndroidAssistantLauncher(context: Context) : AssistantLauncher {

    private val appContext = context.applicationContext

    override fun launchWakeWord(): AssistantLaunchResult {
        val intent = Intent(Intent.ACTION_ASSIST)
            .setClassName(appContext, MAIN_ACTIVITY_CLASS)
            .putExtra(EXTRA_WAKE_WORD, true)
            .addFlags(
                Intent.FLAG_ACTIVITY_NEW_TASK or
                    Intent.FLAG_ACTIVITY_SINGLE_TOP or
                    Intent.FLAG_ACTIVITY_CLEAR_TOP,
            )
        val startedAt = SystemClock.uptimeMillis()
        return try {
            appContext.startActivity(intent)
            AssistantLaunchResult(
                started = true,
                latencyMillis = SystemClock.uptimeMillis() - startedAt,
            )
        } catch (error: Exception) {
            AssistantLaunchResult(
                started = false,
                failure = error.message?.takeIf { it.isNotBlank() }
                    ?: error.javaClass.simpleName,
                latencyMillis = SystemClock.uptimeMillis() - startedAt,
            )
        }
    }

    private companion object {
        const val MAIN_ACTIVITY_CLASS = "com.example.hermestalk.MainActivity"
    }
}
