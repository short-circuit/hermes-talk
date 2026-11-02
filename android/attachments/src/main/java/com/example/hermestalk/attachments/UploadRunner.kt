package com.example.hermestalk.attachments

import com.example.hermestalk.rtvi.RelaySettings

/** Outcome of a complete retry loop. */
data class UploadRunResult(
    val outcome: UploadOutcome,
    val attempts: Int,
)

/**
 * Runs [UploadTransport.upload] under [RetryPolicy]. The delay function is
 * injectable so tests can assert the requested backoff without sleeping.
 */
class UploadRunner(
    private val engine: UploadTransport,
    private val delayMillis: suspend (Long) -> Unit,
) {

    suspend fun run(
        settings: RelaySettings,
        payload: UploadPayload,
        onProgress: (uploadedBytes: Long, totalBytes: Long) -> Unit,
        onAttempt: (attempt: Int) -> Unit = {},
        onRetry: (nextAttempt: Int, delaySeconds: Long) -> Unit = { _, _ -> },
    ): UploadRunResult {
        var attempt = 0
        while (true) {
            attempt += 1
            onAttempt(attempt)
            when (val outcome = engine.upload(settings, payload, onProgress)) {
                is UploadOutcome.Success -> return UploadRunResult(outcome, attempt)
                is UploadOutcome.Failure -> {
                    when (val decision = RetryPolicy.decide(outcome.failure, attempt)) {
                        is RetryDecision.Retry -> {
                            onRetry(attempt + 1, decision.delaySeconds)
                            delayMillis(decision.delaySeconds * 1000L)
                        }

                        RetryDecision.GiveUp ->
                            return UploadRunResult(outcome, attempt)
                    }
                }
            }
        }
    }
}
