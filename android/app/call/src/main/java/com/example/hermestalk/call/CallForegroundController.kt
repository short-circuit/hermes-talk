package com.example.hermestalk.call

import android.content.Context
import android.content.Intent
import androidx.core.content.ContextCompat

/** Drives the microphone foreground service from the controller. */
interface CallForegroundController {
    /** Start the foreground service. */
    fun start()

    /** Update the notification with the latest call controls. */
    fun update(controls: CallControls)

    /** Stop the foreground service. */
    fun stop()
}

/** Sends explicit intents to [CallForegroundService]. */
class AndroidCallForegroundController(context: Context) : CallForegroundController {

    private val appContext = context.applicationContext

    override fun start() {
        val intent = Intent(appContext, CallForegroundService::class.java)
            .setAction(CallForegroundService.ACTION_START)
        ContextCompat.startForegroundService(appContext, intent)
    }

    override fun update(controls: CallControls) {
        val intent = Intent(appContext, CallForegroundService::class.java)
            .setAction(CallForegroundService.ACTION_START)
            .putExtra(CallForegroundService.EXTRA_DURATION_SECONDS, controls.durationSeconds)
            .putExtra(CallForegroundService.EXTRA_ROUTE, controls.route.name)
            .putExtra(CallForegroundService.EXTRA_MUTED, controls.muted)
        ContextCompat.startForegroundService(appContext, intent)
    }

    override fun stop() {
        val intent = Intent(appContext, CallForegroundService::class.java)
            .setAction(CallForegroundService.ACTION_STOP)
        appContext.startService(intent)
    }
}
