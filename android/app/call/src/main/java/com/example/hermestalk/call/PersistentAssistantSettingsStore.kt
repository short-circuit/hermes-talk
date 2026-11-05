package com.example.hermestalk.call

import android.content.Context

/**
 * Persisted opt-in for the always-on assistant.
 *
 * This is intentionally a plain SharedPreferences store rather than the
 * encrypted token store: a BOOT_COMPLETED receiver runs before the user
 * unlocks the device and must be able to read the flag.
 */
interface PersistentAssistantSettingsStore {
    /** True when the user asked the assistant to stay running. Default false. */
    fun isEnabled(): Boolean

    /** Persist the user's choice. */
    fun setEnabled(enabled: Boolean)
}

/** Production store backed by the plain `hermes_talk_persistent` preferences file. */
class SharedPreferencesPersistentAssistantSettingsStore(
    context: Context,
) : PersistentAssistantSettingsStore {

    private val preferences =
        context.applicationContext.getSharedPreferences(PREFERENCES_FILE, Context.MODE_PRIVATE)

    override fun isEnabled(): Boolean = preferences.getBoolean(KEY_ENABLED, false)

    override fun setEnabled(enabled: Boolean) {
        preferences.edit().putBoolean(KEY_ENABLED, enabled).apply()
    }

    companion object {
        const val PREFERENCES_FILE = "hermes_talk_persistent"
        const val KEY_ENABLED = "assistant_enabled"
    }
}
