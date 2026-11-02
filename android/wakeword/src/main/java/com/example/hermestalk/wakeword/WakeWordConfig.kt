package com.example.hermestalk.wakeword

/** Sensitivity presets map directly to the sherpa-onnx keyword threshold. */
enum class WakeWordSensitivity(val threshold: Float) {
    Sensitive(0.10f),
    Balanced(0.15f),
    Strict(0.25f),
    ;

    companion object {
        /** Map a persisted value to a preset; unknown values fall back to Balanced. */
        fun fromStorage(value: String?): WakeWordSensitivity =
            entries.firstOrNull { it.name.equals(value, ignoreCase = true) } ?: Balanced
    }
}

/** A wake-word configuration field that failed validation. */
enum class WakeWordField {
    Phrase,
    AssetDirectory,
    Sensitivity,
    Score,
    TrailingBlanks,
    MaxActivePaths,
    NumThreads,
    CooldownMillis,
    SampleRate,
    ChunkSamples,
    LogCapBytes,
}

/** A validation error tied to a specific wake-word configuration field. */
data class WakeWordFieldError(val field: WakeWordField, val message: String)

/**
 * Local keyword-spotting configuration. The defaults are the values measured on
 * the host with the bundled gigaspeech int8 model. Validation never throws on
 * user input; it returns every field error instead.
 */
data class WakeWordConfig(
    val phrase: String = "HEY HERMES",
    val assetDirectory: String = "wakeword",
    val sensitivity: WakeWordSensitivity = WakeWordSensitivity.Balanced,
    val score: Float = 1.0f,
    val trailingBlanks: Int = 1,
    val maxActivePaths: Int = 4,
    val numThreads: Int = 1,
    val cooldownMillis: Long = 2500L,
    val captureSampleRate: Int = 16000,
    val chunkSamples: Int = 1600,
    val falseTriggerLogCapBytes: Long = 65536L,
) {
    /** The sherpa-onnx keywords threshold selected by [sensitivity]. */
    val threshold: Float
        get() = sensitivity.threshold

    val keywordsAsset: String
        get() = "$assetDirectory/keywords.txt"

    /** Validate the configuration and return every field error. */
    fun validate(): List<WakeWordFieldError> {
        val errors = mutableListOf<WakeWordFieldError>()

        if (phrase.isBlank()) {
            errors += WakeWordFieldError(WakeWordField.Phrase, "Wake phrase is required")
        } else if (phrase.any { it == '\n' || it == '\r' }) {
            errors += WakeWordFieldError(WakeWordField.Phrase, "Wake phrase must be one line")
        }

        when {
            assetDirectory.isBlank() ->
                errors += WakeWordFieldError(
                    WakeWordField.AssetDirectory,
                    "Asset directory is required",
                )

            assetDirectory.startsWith("/") || assetDirectory.endsWith("/") ->
                errors += WakeWordFieldError(
                    WakeWordField.AssetDirectory,
                    "Asset directory must be relative without a trailing slash",
                )

            assetDirectory.contains("..") ->
                errors += WakeWordFieldError(
                    WakeWordField.AssetDirectory,
                    "Asset directory must not contain '..'",
                )
        }

        if (sensitivity.threshold !in 0.0f..1.0f) {
            errors += WakeWordFieldError(
                WakeWordField.Sensitivity,
                "Keyword threshold must be between 0.0 and 1.0",
            )
        }
        if (score !in 0.0f..10.0f || score.isNaN()) {
            errors += WakeWordFieldError(
                WakeWordField.Score,
                "Keyword score must be between 0.0 and 10.0",
            )
        }
        if (trailingBlanks !in 0..10) {
            errors += WakeWordFieldError(
                WakeWordField.TrailingBlanks,
                "Trailing blanks must be between 0 and 10",
            )
        }
        if (maxActivePaths !in 1..16) {
            errors += WakeWordFieldError(
                WakeWordField.MaxActivePaths,
                "Active paths must be between 1 and 16",
            )
        }
        if (numThreads !in 1..8) {
            errors += WakeWordFieldError(
                WakeWordField.NumThreads,
                "Threads must be between 1 and 8",
            )
        }
        if (cooldownMillis !in 0L..60_000L) {
            errors += WakeWordFieldError(
                WakeWordField.CooldownMillis,
                "Cooldown must be between 0 and 60000 ms",
            )
        }
        if (captureSampleRate !in 8_000..48_000) {
            errors += WakeWordFieldError(
                WakeWordField.SampleRate,
                "Capture sample rate must be between 8000 and 48000 Hz",
            )
        }
        if (chunkSamples !in 1..captureSampleRate) {
            errors += WakeWordFieldError(
                WakeWordField.ChunkSamples,
                "Chunk size must be between 1 and the sample rate",
            )
        }
        if (falseTriggerLogCapBytes !in 1_024L..10_485_760L) {
            errors += WakeWordFieldError(
                WakeWordField.LogCapBytes,
                "False-trigger log cap must be between 1024 and 10485760 bytes",
            )
        }

        return errors
    }

    /** True when [validate] returns no errors. */
    fun isValid(): Boolean = validate().isEmpty()
}
