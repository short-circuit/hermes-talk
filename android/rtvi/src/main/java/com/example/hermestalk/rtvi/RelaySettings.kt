package com.example.hermestalk.rtvi

/** Settings required to reach a relay. The token is a bearer credential. */
data class RelaySettings(val relayUrl: String, val deviceToken: String)

/** A settings field that failed validation. */
enum class Field { RelayUrl, DeviceToken }

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
     */
    fun validate(settings: RelaySettings): List<FieldError> {
        val errors = mutableListOf<FieldError>()

        when (val parsed = RelayEndpoint.parse(settings.relayUrl)) {
            is RelayEndpoint.ParseResult.Invalid ->
                errors += FieldError(Field.RelayUrl, parsed.reason)

            is RelayEndpoint.ParseResult.Valid -> Unit
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

    private fun looksLikePlaceholder(token: String): Boolean {
        val normalized = token.trim().lowercase()
        return normalized in placeholderTokens ||
            normalized.contains("example") ||
            normalized.contains("your")
    }
}
