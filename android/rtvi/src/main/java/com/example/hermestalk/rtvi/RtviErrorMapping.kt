package com.example.hermestalk.rtvi

import ai.pipecat.client.result.HttpError
import ai.pipecat.client.result.RTVIError

/** Maximum length of any error detail shown in the UI. */
internal const val MAX_DETAIL = 200

/**
 * Maps a Pipecat [RTVIError] to the failure category shown by the connection
 * UI. The returned detail never contains the device token.
 */
internal fun mapRtviError(error: RTVIError): Pair<FailureReason, String> = when (error) {
    is RTVIError.HttpError -> {
        val http = error.error
        if (http is HttpError.BadStatusCode) {
            when (http.code) {
                401, 403 -> FailureReason.Unauthorized to
                    "Relay rejected the device token (HTTP ${http.code})"

                else -> FailureReason.HandshakeFailed to
                    "Relay returned HTTP ${http.code}"
            }
        } else {
            FailureReason.HandshakeFailed to http.description.take(MAX_DETAIL)
        }
    }

    is RTVIError.OtherError ->
        FailureReason.HandshakeFailed to error.message.take(MAX_DETAIL)

    is RTVIError.ExceptionThrown ->
        FailureReason.HandshakeFailed to
            (error.exception.message ?: "Transport exception").take(MAX_DETAIL)

    is RTVIError.Timeout ->
        FailureReason.Unreachable to "Timed out waiting for the relay"

    is RTVIError.OperationCancelled ->
        FailureReason.TransportClosed to "The connection was cancelled"

    is RTVIError.ErrorResponse ->
        FailureReason.BackendError to error.message.take(MAX_DETAIL)

    is RTVIError.PreviousConnectionStillActive ->
        FailureReason.Unexpected to "A previous connection is still active"

    is RTVIError.TransportNotInitialized ->
        FailureReason.HandshakeFailed to "The transport is not initialized"

    is RTVIError.InvalidState ->
        FailureReason.Unexpected to error.description.take(MAX_DETAIL)

    else ->
        FailureReason.Unexpected to error.description.take(MAX_DETAIL)
}
