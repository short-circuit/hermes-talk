package com.example.hermestalk.call

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.core.content.ContextCompat

/**
 * Restarts the persistent assistant after a reboot or an app update when the
 * user enabled it.
 *
 * `LOCKED_BOOT_COMPLETED` is deliberately not handled: the enabled flag lives in
 * credential-protected storage and is not readable before the user unlocks.
 * Android 14+ refuses a microphone foreground service from BOOT_COMPLETED; the
 * service itself records that refusal and falls back to the special-use type.
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action
        if (action != Intent.ACTION_BOOT_COMPLETED &&
            action != Intent.ACTION_MY_PACKAGE_REPLACED
        ) {
            return
        }

        val store = SharedPreferencesPersistentAssistantSettingsStore(context)
        if (!store.isEnabled()) {
            Log.i(TAG, "$action received; persistent assistant is disabled")
            return
        }

        val serviceIntent = Intent(context, PersistentAssistantService::class.java)
            .setAction(PersistentAssistantService.ACTION_START)
        try {
            ContextCompat.startForegroundService(context, serviceIntent)
            Log.i(TAG, "$action received; persistent assistant start requested")
        } catch (error: Exception) {
            if (PersistentAssistant.isMicRefusal(error)) {
                Log.w(
                    TAG,
                    "$action refused the microphone foreground start; " +
                        "the service will fall back to special use",
                    error,
                )
            } else {
                Log.w(TAG, "$action could not start the persistent assistant", error)
            }
        }
    }

    private companion object {
        const val TAG = "PersistentAssistantBoot"
    }
}
