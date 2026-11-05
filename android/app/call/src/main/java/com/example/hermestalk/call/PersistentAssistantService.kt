package com.example.hermestalk.call

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat

/**
 * Always-on assistant foreground service.
 *
 * Unlike [CallForegroundService], this service is not scoped to a call:
 * `stopWithTask` is false and the service returns [Service.START_STICKY], so it
 * survives a task swipe and a system restart. It is started by the Settings
 * toggle, by [BootReceiver] when the user enabled it, or by the app on resume.
 *
 * On Android 14+ a microphone foreground service cannot be started from the
 * background (for example from BOOT_COMPLETED). The service therefore tries the
 * microphone type first, records a refusal in [PersistentAssistant], and falls
 * back synchronously to the special-use type so it never misses the 5-second
 * startForeground window. [ACTION_PROMOTE_MIC] retries the microphone type when
 * the app becomes visible again.
 */
class PersistentAssistantService : Service() {

    private var wakeLock: PowerManager.WakeLock? = null
    private var listening = false
    private var foregroundStarted = false
    private var foregroundType = AssistantForegroundType.None

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                ensureForeground()
                stopCleanly()
            }

            ACTION_PROMOTE_MIC -> {
                ensureForeground(retryMicrophone = true)
                updateForegroundNotification()
            }

            ACTION_SET_LISTENING -> {
                listening = intent.getBooleanExtra(EXTRA_LISTENING, false)
                if (foregroundStarted) {
                    updateForegroundNotification()
                } else {
                    ensureForeground()
                }
                applyWakeLockPolicy()
            }

            ACTION_START, null -> {
                listening = false
                ensureForeground()
                applyWakeLockPolicy()
            }

            else -> {
                if (!foregroundStarted) {
                    listening = false
                    ensureForeground()
                }
                applyWakeLockPolicy()
            }
        }
        PersistentAssistant.update { it.copy(listening = listening) }
        return PersistentAssistant.restartMode()
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        // stopWithTask=false keeps the process alive; log only, never stop.
        Log.i(TAG, "Task removed; persistent assistant stays running")
    }

    override fun onDestroy() {
        releaseWakeLock()
        foregroundStarted = false
        foregroundType = AssistantForegroundType.None
        listening = false
        PersistentAssistant.reset()
        Log.i(TAG, "Persistent assistant service destroyed")
        super.onDestroy()
    }

    /**
     * Start (or refresh) the foreground notification. The order comes from the
     * pure policy: microphone first when RECORD_AUDIO is granted on API 29+,
     * then specialUse. A plain startForeground is the final API < 29 fallback.
     */
    private fun ensureForeground(retryMicrophone: Boolean = true) {
        if (foregroundStarted && !retryMicrophone) {
            updateForegroundNotification()
            return
        }

        val order = PersistentAssistant.attemptOrder(
            hasRecordAudio = hasRecordAudio(),
            sdkInt = Build.VERSION.SDK_INT,
        )
        val accepted = PersistentAssistant.firstSuccessful(
            candidates = order,
            attempt = { type -> startForegroundFor(type) },
            onFailure = { type, error ->
                if (type == AssistantForegroundType.Microphone &&
                    PersistentAssistant.isMicRefusal(error)
                ) {
                    val record = PersistentAssistant.micRefusalRecord(error)
                    PersistentAssistant.update { it.copy(micRefusal = record) }
                    Log.w(
                        TAG,
                        "Microphone foreground type refused ($record); falling back",
                        error,
                    )
                } else {
                    Log.w(
                        TAG,
                        "startForeground(${type.name}) failed; trying the next type",
                        error,
                    )
                }
            },
        )
        if (accepted != null) {
            onForegroundStarted(accepted)
            return
        }

        // Last resort: never leave the service outside the foreground state.
        try {
            startForeground(NOTIFICATION_ID, buildNotification())
            onForegroundStarted(AssistantForegroundType.None)
        } catch (error: Exception) {
            Log.e(TAG, "Could not enter the foreground state", error)
        }
    }

    /** Start the foreground state with [type], letting the exception propagate. */
    private fun startForegroundFor(type: AssistantForegroundType) {
        val notification = buildNotification()
        if (type == AssistantForegroundType.None) {
            startForeground(NOTIFICATION_ID, notification)
        } else {
            startForeground(
                NOTIFICATION_ID,
                notification,
                type.toServiceInfoType(),
            )
        }
    }

    private fun onForegroundStarted(type: AssistantForegroundType) {
        foregroundStarted = true
        foregroundType = type
        if (type == AssistantForegroundType.Microphone) {
            PersistentAssistant.update { it.copy(micRefusal = null) }
        }
        PersistentAssistant.update { state ->
            state.copy(
                running = true,
                foregroundType = type,
                listening = listening,
                startedAtMillis = if (state.running) {
                    state.startedAtMillis
                } else {
                    System.currentTimeMillis()
                },
            )
        }
        applyWakeLockPolicy()
    }

    private fun updateForegroundNotification() {
        if (!foregroundStarted) {
            ensureForeground()
            return
        }
        try {
            startForegroundFor(foregroundType)
        } catch (error: Exception) {
            Log.w(TAG, "Could not update the foreground notification", error)
            ensureForeground(retryMicrophone = true)
        }
    }

    private fun applyWakeLockPolicy() {
        if (PersistentAssistant.shouldHoldWakeLock(listening)) {
            acquireWakeLock()
        } else {
            releaseWakeLock()
        }
        PersistentAssistant.update {
            it.copy(listening = listening, wakeLockHeld = wakeLock?.isHeld == true)
        }
    }

    private fun acquireWakeLock() {
        if (wakeLock?.isHeld == true) {
            return
        }
        try {
            val powerManager =
                getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = powerManager
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, WAKE_LOCK_TAG)
                .apply {
                    setReferenceCounted(false)
                    acquire()
                }
        } catch (error: Exception) {
            Log.w(TAG, "Could not acquire the listening wake lock", error)
            wakeLock = null
        }
    }

    private fun releaseWakeLock() {
        try {
            wakeLock?.let { if (it.isHeld) it.release() }
        } catch (error: Exception) {
            Log.w(TAG, "Could not release the listening wake lock", error)
        }
        wakeLock = null
    }

    private fun stopCleanly() {
        releaseWakeLock()
        try {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } catch (error: Exception) {
            Log.w(TAG, "Could not remove the foreground notification", error)
        }
        foregroundStarted = false
        foregroundType = AssistantForegroundType.None
        listening = false
        PersistentAssistant.reset()
        stopSelf()
    }

    private fun hasRecordAudio(): Boolean = ContextCompat.checkSelfPermission(
        this,
        Manifest.permission.RECORD_AUDIO,
    ) == PackageManager.PERMISSION_GRANTED

    private fun createNotificationChannel() {
        try {
            val manager =
                getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            val channel = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.persistent_assistant_channel_name),
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description =
                    getString(R.string.persistent_assistant_channel_description)
                setShowBadge(false)
            }
            manager.createNotificationChannel(channel)
        } catch (error: Exception) {
            Log.w(TAG, "Could not create the assistant notification channel", error)
        }
    }

    private fun buildNotification(): Notification {
        val launchIntent = Intent()
            .setClassName(this, MAIN_ACTIVITY_CLASS)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        val contentIntent = PendingIntent.getActivity(
            this,
            REQUEST_CONTENT,
            launchIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        val stopIntent = Intent(this, PersistentAssistantService::class.java)
            .setAction(ACTION_STOP)
        val stopPending = PendingIntent.getService(
            this,
            REQUEST_STOP,
            stopIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle(getString(R.string.persistent_notification_title))
            .setContentText(getString(PersistentAssistant.notificationText(listening)))
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .setContentIntent(contentIntent)
            .addAction(
                android.R.drawable.ic_menu_close_clear_cancel,
                getString(R.string.persistent_notification_stop),
                stopPending,
            )
            .build()
    }

    companion object {
        const val ACTION_START = "com.example.hermestalk.call.action.START_ASSISTANT"
        const val ACTION_STOP = "com.example.hermestalk.call.action.STOP_ASSISTANT"
        const val ACTION_PROMOTE_MIC = "com.example.hermestalk.call.action.PROMOTE_MIC"
        const val ACTION_SET_LISTENING =
            "com.example.hermestalk.call.action.SET_ASSISTANT_LISTENING"

        const val EXTRA_LISTENING = "com.example.hermestalk.call.extra.LISTENING"

        const val NOTIFICATION_ID = 4201
        const val CHANNEL_ID = "hermes_talk_assistant"

        private const val TAG = "PersistentAssistant"
        private const val MAIN_ACTIVITY_CLASS = "com.example.hermestalk.MainActivity"
        private const val WAKE_LOCK_TAG = "hermes_talk:assistant-listening"
        private const val REQUEST_CONTENT = 4201
        private const val REQUEST_STOP = 4202
    }
}
