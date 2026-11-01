package com.example.hermestalk.rtvi

import java.net.URI
import java.net.URISyntaxException

/**
 * A validated relay endpoint. Instances are only produced by [parse], so the
 * scheme, host, optional port and normalized path are safe to concatenate.
 *
 * @property scheme normalized lowercase scheme, either `http` or `https`.
 * @property host hostname without IPv6 brackets.
 * @property port optional TCP port in the range 1..65535.
 * @property path normalized path prefix: empty, or starts with `/` and has no
 *   trailing slash.
 */
data class RelayEndpoint(val scheme: String, val host: String, val port: Int?, val path: String) {

    /** Absolute base URL for the relay, without a trailing slash. */
    fun baseUrl(): String {
        val authority = if (host.contains(':')) "[$host]" else host
        val portPart = port?.let { ":$it" } ?: ""
        return "$scheme://$authority$portPart$path"
    }

    /** URL used by the SmallWebRTC transport to start a session. */
    fun startUrl(): String = baseUrl() + "/start"

    /** Liveness probe URL. This does not prove RTVI readiness. */
    fun healthUrl(): String = baseUrl() + "/health"

    /** True when the relay is addressed through the loopback interface. */
    val isLoopback: Boolean
        get() = host == "127.0.0.1" || host == "::1" || host.equals("localhost", ignoreCase = true)

    /** True when the relay is addressed over plain HTTP. */
    val isCleartext: Boolean
        get() = scheme == "http"

    /** Result of parsing a user-entered relay URL. */
    sealed interface ParseResult {
        /** The URL is valid. */
        data class Valid(val endpoint: RelayEndpoint) : ParseResult

        /** The URL is invalid; [reason] is short and UI-friendly. */
        data class Invalid(val reason: String) : ParseResult
    }

    companion object {
        /**
         * Parse and validate a relay URL. Blank input is always invalid.
         * Userinfo, query strings, fragments and out-of-range ports are
         * rejected. A single trailing slash on a path prefix is removed.
         */
        fun parse(raw: String): ParseResult {
            val trimmed = raw.trim()
            if (trimmed.isEmpty()) {
                return ParseResult.Invalid("Relay URL is required")
            }

            val uri = try {
                URI(trimmed)
            } catch (_: URISyntaxException) {
                return ParseResult.Invalid("Relay URL is not a valid URL")
            }

            val scheme = uri.scheme?.lowercase()
                ?: return ParseResult.Invalid("Relay URL must start with http:// or https://")
            if (scheme != "http" && scheme != "https") {
                return ParseResult.Invalid("Relay URL must use http or https")
            }
            if (uri.rawUserInfo != null) {
                return ParseResult.Invalid("Relay URL must not contain credentials")
            }
            if (uri.rawQuery != null) {
                return ParseResult.Invalid("Relay URL must not contain a query string")
            }
            if (uri.rawFragment != null) {
                return ParseResult.Invalid("Relay URL must not contain a fragment")
            }

            val rawHost = uri.host
                ?: return ParseResult.Invalid("Relay URL is missing a host")
            val host = rawHost.removePrefix("[").removeSuffix("]")
            if (host.isBlank()) {
                return ParseResult.Invalid("Relay URL is missing a host")
            }

            val port = uri.port
            if (port == -1) {
                // No explicit port; valid.
            } else if (port < 1 || port > 65535) {
                return ParseResult.Invalid("Relay port must be between 1 and 65535")
            }

            val path = normalizePath(uri.rawPath)
                ?: return ParseResult.Invalid("Relay path must start with /")

            return ParseResult.Valid(
                RelayEndpoint(
                    scheme = scheme,
                    host = host,
                    port = if (port == -1) null else port,
                    path = path,
                ),
            )
        }

        private fun normalizePath(rawPath: String?): String? {
            val path = rawPath ?: return ""
            if (path.isEmpty() || path == "/") {
                return ""
            }
            if (!path.startsWith("/")) {
                return null
            }
            val withoutTrailingSlash = if (path.endsWith("/")) path.dropLast(1) else path
            return if (withoutTrailingSlash == "/") "" else withoutTrailingSlash
        }
    }
}
