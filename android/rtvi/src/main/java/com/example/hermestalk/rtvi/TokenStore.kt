package com.example.hermestalk.rtvi

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * Persistent storage for [RelaySettings]. Implementations must never expose the
 * device token in logs.
 */
interface TokenStore {
    /** Return the stored settings, or null when either value is missing/blank. */
    fun read(): RelaySettings?

    /** Persist [settings]. */
    fun write(settings: RelaySettings)

    /** Remove all stored settings. */
    fun clear()
}

/**
 * EncryptedSharedPreferences-backed store. The master key uses AES256-GCM and
 * keys/values are encrypted with AES256-SIV/AES256-GCM respectively.
 *
 * Failures (for example a corrupt keystore after an app reinstall) are surfaced
 * as null reads and recorded in [lastError] instead of crashing startup.
 */
class EncryptedTokenStore(context: Context) : TokenStore {

    private val appContext = context.applicationContext
    private var error: String? = null
    private val preferences: SharedPreferences? = try {
        val masterKey = MasterKey.Builder(appContext)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        EncryptedSharedPreferences.create(
            appContext,
            FILE_NAME,
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    } catch (failure: Exception) {
        error = failure.message ?: "Could not open encrypted settings"
        null
    }

    /** Last storage error, or null when storage is healthy. */
    fun lastError(): String? = error

    override fun read(): RelaySettings? = try {
        val url = preferences?.getString(KEY_RELAY_URL, null)
        val token = preferences?.getString(KEY_DEVICE_TOKEN, null)
        if (url.isNullOrBlank() || token.isNullOrBlank()) {
            null
        } else {
            RelaySettings(url, token)
        }
    } catch (failure: Exception) {
        error = failure.message ?: "Could not read encrypted settings"
        null
    }

    override fun write(settings: RelaySettings) {
        val store = preferences
        if (store == null) {
            error = error ?: "Encrypted settings storage is unavailable"
            return
        }
        try {
            store.edit()
                .putString(KEY_RELAY_URL, settings.relayUrl)
                .putString(KEY_DEVICE_TOKEN, settings.deviceToken)
                .apply()
            error = null
        } catch (failure: Exception) {
            error = failure.message ?: "Could not write encrypted settings"
        }
    }

    override fun clear() {
        val store = preferences
        if (store == null) {
            return
        }
        try {
            store.edit().clear().apply()
            error = null
        } catch (failure: Exception) {
            error = failure.message ?: "Could not clear encrypted settings"
        }
    }

    private companion object {
        const val FILE_NAME = "hermes_talk_settings"
        const val KEY_RELAY_URL = "relay_url"
        const val KEY_DEVICE_TOKEN = "device_token"
    }
}

/** Simple in-memory store used by tests and previews. */
class InMemoryTokenStore : TokenStore {

    private var settings: RelaySettings? = null

    override fun read(): RelaySettings? = settings

    override fun write(settings: RelaySettings) {
        this.settings = settings
    }

    override fun clear() {
        settings = null
    }
}
