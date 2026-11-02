package com.example.hermestalk.wakeword

import android.content.Context

/** Persisted wake-word preferences. */
data class WakeWordSettings(
    val enabled: Boolean = false,
    val sensitivity: WakeWordSensitivity = WakeWordSensitivity.Balanced,
)

/** Storage seam so the controller and tests do not depend on SharedPreferences. */
interface WakeWordSettingsStore {
    fun load(): WakeWordSettings
    fun save(settings: WakeWordSettings)
}

/** Production store backed by private SharedPreferences. */
class SharedPreferencesWakeWordSettingsStore(context: Context) : WakeWordSettingsStore {

    private val preferences =
        context.getSharedPreferences("wakeword_settings", Context.MODE_PRIVATE)

    override fun load(): WakeWordSettings = WakeWordSettings(
        enabled = preferences.getBoolean(KEY_ENABLED, false),
        sensitivity = WakeWordSensitivity.fromStorage(preferences.getString(KEY_SENSITIVITY, null)),
    )

    override fun save(settings: WakeWordSettings) {
        preferences.edit()
            .putBoolean(KEY_ENABLED, settings.enabled)
            .putString(KEY_SENSITIVITY, settings.sensitivity.name)
            .apply()
    }

    private companion object {
        const val KEY_ENABLED = "enabled"
        const val KEY_SENSITIVITY = "sensitivity"
    }
}

/** In-memory store used by unit tests and by callers that do not persist. */
class InMemoryWakeWordSettingsStore(
    initial: WakeWordSettings = WakeWordSettings(),
) : WakeWordSettingsStore {
    private var settings = initial

    override fun load(): WakeWordSettings = settings

    override fun save(settings: WakeWordSettings) {
        this.settings = settings
    }
}
