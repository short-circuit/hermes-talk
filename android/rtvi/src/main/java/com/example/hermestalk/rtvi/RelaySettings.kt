package com.example.hermestalk.rtvi

/**
 * Settings required to reach a relay. The token is a bearer credential.
 *
 * [tlsPin] is the canonical or user-entered SPKI pin for the relay leaf
 * certificate. [trustPlatformCa] is the explicit opt-in to use the Android CA
 * store instead of pinning; a pin still wins when both are set.
 */
data class RelaySettings(
    val relayUrl: String,
    val deviceToken: String,
    val tlsPin: String = "",
    val trustPlatformCa: Boolean = false,
)

/** A settings field that failed validation. */
enum class Field { RelayUrl, DeviceToken, TlsPin }

/** A validation error tied to a specific settings field. */
data class FieldError(val field: Field, val message: String)

/**
 * Pure validation for relay settings. Messages are deliberately short so they
 * can be shown inline next to the corresponding input field.
 */
object RelaySettingsValidator {

    private val placeholderTokens = setOf(
        "changeme",
        "change-me",
        "your-token",
        "your_token",
        "paste-token-here",
        "replace-me",
        "placeholder",
        "token",
        "xxx",
        "todo",
        "<token>",
        "bearer",
    )

    /**
     * Validate [settings] and return every field error. An empty list means the
     * settings are usable. Short tokens are accepted; only blank tokens,
     * tokens containing whitespace and obvious placeholders are rejected.
     *
     * Plain HTTP is accepted only on a loopback host (the adb-reverse
     * development path). HTTPS must be pinned unless the user explicitly opted
     * into the platform CA store. A pin set on a plain-HTTP URL is allowed but
     * ignored.
     */
    fun validate(settings: RelaySettings): List<FieldError> {
        val errors = mutableListOf<FieldError>()

        when (val parsed = RelayEndpoint.parse(settings.relayUrl)) {
            is RelayEndpoint.ParseResult.Invalid ->
                errors += FieldError(Field.RelayUrl, parsed.reason)

            is RelayEndpoint.ParseResult.Valid -> {
                val endpoint = parsed.endpoint
                if (endpoint.isCleartext) {
                    if (!endpoint.isLoopback) {
                        errors += FieldError(
                            Field.RelayUrl,
                            "Plain HTTP is only allowed on the loopback interface (use adb reverse)",
                        )
                    }
                } else {
                    validateTls(endpoint, settings, errors)
                }
            }
        }

        val token = settings.deviceToken
        when {
            token.isBlank() ->
                errors += FieldError(Field.DeviceToken, "Device token is required")

            token.any { it.isWhitespace() } ->
                errors += FieldError(Field.DeviceToken, "Device token must not contain spaces")

            looksLikePlaceholder(token) ->
                errors += FieldError(Field.DeviceToken, "Replace the example device token")
        }

        return errors
    }

    private fun validateTls(
        endpoint: RelayEndpoint,
        settings: RelaySettings,
        errors: MutableList<FieldError>,
    ) {
        // Only HTTPS reaches this branch; the endpoint is validated already.
        val normalized = SpkiPin.normalize(settings.tlsPin)
        when {
            settings.trustPlatformCa -> {
                // A pin is optional here, but if one is supplied it must be
                // usable because it takes precedence over the platform store.
                if (settings.tlsPin.isNotBlank() && normalized == null) {
                    errors += FieldError(
                        Field.TlsPin,
                        "Pin must be a SHA-256 SPKI hash (sha256/<base64>)",
                    )
                }
            }

            settings.tlsPin.isBlank() ->
                errors += FieldError(
                    Field.TlsPin,
                    "Pin the relay certificate (fetch it from the relay)",
                )

            normalized == null ->
                errors += FieldError(
                    Field.TlsPin,
                    "Pin must be a SHA-256 SPKI hash (sha256/<base64>)",
                )
        }
    }

    private fun looksLikePlaceholder(token: String): Boolean {
        val normalized = token.trim().lowercase()
        return normalized in placeholderTokens ||
            normalized.contains("example") ||
            normalized.contains("your")
    }
}

/**
 * Map [RelaySettings] to the TLS policy used by probes. This never returns a
 * trust-all policy: plain HTTP is only mapped to [TlsPolicy.CleartextLoopback]
 * for a loopback endpoint, and HTTPS falls back to [TlsPolicy.PlatformCa] when
 * no usable pin is configured. Validation rejects an HTTPS endpoint with
 * neither a pin nor the explicit platform-CA opt-in before this is used.
 */
fun RelaySettings.tlsPolicy(): TlsPolicy {
    val endpoint = (RelayEndpoint.parse(relayUrl) as? RelayEndpoint.ParseResult.Valid)?.endpoint
        ?: return TlsPolicy.PlatformCa
    if (endpoint.isCleartext) {
        return TlsPolicy.CleartextLoopback
    }
    val normalized = SpkiPin.normalize(tlsPin)
    if (normalized != null) {
        return TlsPolicy.Pinned(normalized)
    }
    return TlsPolicy.PlatformCa
}
