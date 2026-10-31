package com.example.hermestalk.spike

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log

/**
 * Foreground service that owns the call.
 *
 * Manifest declares `foregroundServiceType="microphone|mediaPlayback"`; Android 14 requires the
 * matching `FOREGROUND_SERVICE_MICROPHONE`/`FOREGROUND_SERVICE_MEDIA_PLAYBACK` permissions and a
 * granted `RECORD_AUDIO` before `startForeground()` may pass the microphone type.
 */
class CallService : Service() {

    private var engine: AudioEngine? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        Log.i(TAG, "service: created")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopCall()
            return START_NOT_STICKY
        }
        startCall(intent)
        return START_STICKY
    }

    override fun onDestroy() {
        engine?.stop()
        engine = null
        isRunning = false
        Log.i(TAG, "service: destroyed")
        super.onDestroy()
    }

    private fun startCall(intent: Intent?) {
        val host = intent?.getStringExtra(EXTRA_HOST) ?: DEFAULT_HOST
        val port = intent?.getIntExtra(EXTRA_PORT, DEFAULT_PORT) ?: DEFAULT_PORT
        val toneHz = intent?.getDoubleExtra(EXTRA_TONE_HZ, 0.0) ?: 0.0
        val mediaUsage = intent?.getBooleanExtra(EXTRA_MEDIA_USAGE, false) ?: false

        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "service: RECORD_AUDIO not granted; a microphone foreground service would throw")
        }

        createChannel()
        val notification = buildNotification(host, port)
        val types = ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE or
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(NOTIFICATION_ID, notification, types)
            } else {
                startForeground(NOTIFICATION_ID, notification)
            }
            Log.i(TAG, "service: foreground started type=microphone|mediaPlayback")
        } catch (t: Throwable) {
            Log.e(TAG, "service: startForeground failed: ${t.javaClass.simpleName}: ${t.message}")
            stopSelf()
            return
        }

        if (engine == null) {
            engine = AudioEngine(applicationContext, host, port, toneHz, mediaUsage).also { it.start() }
        }
        isRunning = true
    }

    private fun stopCall() {
        Log.i(TAG, "service: stop requested")
        engine?.stop()
        engine = null
        isRunning = false
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } else {
            @Suppress("DEPRECATION")
            stopForeground(true)
        }
        stopSelf()
    }

    private fun createChannel() {
        val manager = getSystemService(NotificationManager::class.java) ?: return
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return
        val channel = NotificationChannel(CHANNEL_ID, "Hermes Talk call", NotificationManager.IMPORTANCE_LOW)
        channel.description = "Microphone call in progress"
        channel.setSound(null, null)
        manager.createNotificationChannel(channel)
    }

    private fun buildNotification(host: String, port: Int): Notification {
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val stop = PendingIntent.getService(
            this,
            1,
            Intent(this, CallService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return builder
            .setContentTitle("Hermes Talk spike call")
            .setContentText("relay $host:$port")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "Stop", stop)
            .build()
    }

    companion object {
        const val ACTION_START = "com.example.hermestalk.spike.START"
        const val ACTION_STOP = "com.example.hermestalk.spike.STOP"
        const val EXTRA_HOST = "host"
        const val EXTRA_PORT = "port"
        const val EXTRA_TONE_HZ = "tone_hz"
        const val EXTRA_MEDIA_USAGE = "media_usage"
        const val EXTRA_AUTOSTART = "autostart"
        const val CHANNEL_ID = "hermes_talk_spike_call"
        const val NOTIFICATION_ID = 42

        @Volatile
        var isRunning: Boolean = false
            private set
    }
}
