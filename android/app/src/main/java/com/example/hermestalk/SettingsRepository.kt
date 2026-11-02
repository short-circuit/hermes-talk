package com.example.hermestalk

import com.example.hermestalk.rtvi.FieldError
import com.example.hermestalk.rtvi.RelaySettings
import com.example.hermestalk.rtvi.RelaySettingsValidator
import com.example.hermestalk.rtvi.TokenStore

/**
 * Persists and validates relay settings. The token is only exposed through
 * [load] for masked display; it is never logged here.
 */
class SettingsRepository(private val tokenStore: TokenStore) {

    /** Return the stored settings, or null when nothing usable is stored. */
    fun load(): RelaySettings? = tokenStore.read()

    /**
     * Validate and persist [settings]. Returns the field errors; when the list
     * is non-empty nothing is written, so a blank token cannot overwrite a
     * previously saved value.
     */
    fun save(settings: RelaySettings): List<FieldError> {
        val errors = RelaySettingsValidator.validate(settings)
        if (errors.isEmpty()) {
            tokenStore.write(
                RelaySettings(
                    relayUrl = settings.relayUrl.trim(),
                    deviceToken = settings.deviceToken.trim(),
                    tlsPin = settings.tlsPin.trim(),
                    trustPlatformCa = settings.trustPlatformCa,
                ),
            )
        }
        return errors
    }

    /** Remove all persisted settings. */
    fun clear() {
        tokenStore.clear()
    }
}
