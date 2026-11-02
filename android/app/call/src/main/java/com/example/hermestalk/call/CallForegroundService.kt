package com.example.hermestalk.call

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat

/**
 * Foreground service that keeps the microphone session alive while the screen
 * is off.
 *
 * `stopWithTask` is true in the manifest: swiping the task away ends the call
 * and releases the microphone rather than leaving an invisible recording.
 */
class CallForegroundService : Service() {

    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var lastControls = CallControls()
    private var startedForeground = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> startCall(intent)
            ACTION_TOGGLE_MUTE -> toggleMute()
            ACTION_END_CALL -> endCall()
            ACTION_STOP -> stopCleanly()
            else -> if (!startedForeground) {
                startCall(intent)
            }
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        releaseLocks()
        startedForeground = false
        running = false
        foregroundType = 0
        super.onDestroy()
    }

    private fun startCall(intent: Intent?) {
        lastControls = lastControls.copy(
            durationSeconds = intent?.getLongExtra(
                EXTRA_DURATION_SECONDS,
                lastControls.durationSeconds,
            ) ?: lastControls.durationSeconds,
            route = intent?.getStringExtra(EXTRA_ROUTE)
                ?.let { name -> AudioRoute.values().firstOrNull { it.name == name } }
                ?: lastControls.route,
            muted = intent?.getBooleanExtra(EXTRA_MUTED, lastControls.muted)
                ?: lastControls.muted,
        )
        acquireLocks()
        val notification = buildNotification(lastControls)
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(
                    NOTIFICATION_ID,
                    notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE,
                )
            } else {
                startForeground(NOTIFICATION_ID, notification)
            }
            startedForeground = true
            running = true
            foregroundType = ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
        } catch (_: SecurityException) {
            // The permission was revoked between the check and startForeground.
            stopCleanly()
        } catch (_: Exception) {
            stopCleanly()
        }
    }

    private fun toggleMute() {
        try {
            CallControllers.get(applicationContext).toggleMute()
        } catch (_: Exception) {
            // The controller never throws to the service either.
        }
    }

    private fun endCall() {
        try {
            CallControllers.get(applicationContext).endCall()
        } catch (_: Exception) {
            // Fall through to stopping the service.
        }
        stopCleanly()
    }

    private fun acquireLocks() {
        try {
            if (wakeLock?.isHeld != true) {
                val powerManager =
                    getSystemService(Context.POWER_SERVICE) as PowerManager
                wakeLock = powerManager
                    .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "hermes_talk:call")
                    .apply {
                        setReferenceCounted(false)
                        acquire(WAKE_LOCK_TIMEOUT_MS)
                    }
            }
        } catch (_: Exception) {
            // A missing wakelock must not prevent the call from starting.
        }

        try {
            if (wifiLock?.isHeld != true) {
                val wifiManager =
                    applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
                wifiLock = wifiManager
                    .createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "hermes_talk:call")
                    .apply {
                        setReferenceCounted(false)
                        acquire()
                    }
            }
        } catch (_: Exception) {
            // A missing wifilock must not prevent the call from starting.
        }
    }

    private fun releaseLocks() {
        try {
            wakeLock?.let { if (it.isHeld) it.release() }
        } catch (_: Exception) {
            // Best effort.
        }
        wakeLock = null
        try {
            wifiLock?.let { if (it.isHeld) it.release() }
        } catch (_: Exception) {
            // Best effort.
        }
        wifiLock = null
    }

    private fun stopCleanly() {
        releaseLocks()
        try {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } catch (_: Exception) {
            // The service may already be outside the foreground state.
        }
        startedForeground = false
        running = false
        foregroundType = 0
        stopSelf()
    }

    private fun createNotificationChannel() {
        try {
            val manager =
                getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            val channel = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.call_notification_channel_name),
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = getString(R.string.call_notification_channel_description)
                setShowBadge(false)
            }
            manager.createNotificationChannel(channel)
        } catch (_: Exception) {
            // A missing channel still allows startForeground on API 26+.
        }
    }

    private fun buildNotification(controls: CallControls): Notification {
        val launchIntent = packageManager.getLaunchIntentForPackage(packageName)
            ?: Intent(Intent.ACTION_MAIN)
                .setClassName(packageName, "com.example.hermestalk.MainActivity")
        launchIntent.addFlags(
            Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP,
        )
        val contentIntent = PendingIntent.getActivity(
            this,
            0,
            launchIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        val muteIntent = Intent(this, CallForegroundService::class.java)
            .setAction(ACTION_TOGGLE_MUTE)
        val mutePending = PendingIntent.getService(
            this,
            1,
            muteIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val endIntent = Intent(this, CallForegroundService::class.java)
            .setAction(ACTION_END_CALL)
        val endPending = PendingIntent.getService(
            this,
            2,
            endIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        val muteLabel = if (controls.muted) {
            getString(R.string.unmute)
        } else {
            getString(R.string.mute)
        }
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle(getString(R.string.call_notification_title))
            .setContentText(
                getString(
                    R.string.call_notification_text,
                    CallTimer.format(controls.durationSeconds),
                    routeLabel(controls.route),
                ),
            )
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .setContentIntent(contentIntent)
            .addAction(android.R.drawable.ic_lock_silent_mode, muteLabel, mutePending)
            .addAction(
                android.R.drawable.ic_menu_close_clear_cancel,
                getString(R.string.end_call),
                endPending,
            )
            .build()
    }

    private fun routeLabel(route: AudioRoute): String = when (route) {
        AudioRoute.Earpiece -> getString(R.string.route_earpiece)
        AudioRoute.Speaker -> getString(R.string.route_speaker)
        AudioRoute.WiredHeadset -> getString(R.string.route_wired_headset)
        AudioRoute.Bluetooth -> getString(R.string.route_bluetooth)
    }

    companion object {
        const val ACTION_START = "com.example.hermestalk.call.action.START"
        const val ACTION_TOGGLE_MUTE = "com.example.hermestalk.call.action.TOGGLE_MUTE"
        const val ACTION_END_CALL = "com.example.hermestalk.call.action.END_CALL"
        const val ACTION_STOP = "com.example.hermestalk.call.action.STOP"

        const val EXTRA_DURATION_SECONDS =
            "com.example.hermestalk.call.extra.DURATION_SECONDS"
        const val EXTRA_ROUTE = "com.example.hermestalk.call.extra.ROUTE"
        const val EXTRA_MUTED = "com.example.hermestalk.call.extra.MUTED"

        const val NOTIFICATION_ID = 1001
        const val CHANNEL_ID = "hermes_talk_call"

        private const val WAKE_LOCK_TIMEOUT_MS = 60L * 60L * 1000L

        @Volatile
        private var running = false

        @Volatile
        private var foregroundType = 0

        /** Test hook: true while the foreground service owns the microphone. */
        @JvmStatic
        fun isRunning(): Boolean = running

        /** Test hook: the foreground service type in use, or 0 when stopped. */
        @JvmStatic
        fun foregroundType(): Int = foregroundType
    }
}
