package com.example.hermestalk.wakeword

import android.content.Context
import com.k2fsa.sherpa.onnx.FeatureConfig
import com.k2fsa.sherpa.onnx.KeywordSpotter
import com.k2fsa.sherpa.onnx.KeywordSpotterConfig
import com.k2fsa.sherpa.onnx.OnlineModelConfig
import com.k2fsa.sherpa.onnx.OnlineStream
import com.k2fsa.sherpa.onnx.OnlineTransducerModelConfig

/** Typed failure raised when the engine cannot be created or used. */
class WakeWordEngineException(
    message: String,
    cause: Throwable? = null,
) : Exception(message, cause)

/** Seam used by the controller so tests can replace the native engine. */
interface WakeWordKeywordEngine {
    fun newStream()
    fun accept(samples: FloatArray, sampleRate: Int)
    fun poll(): String?
    fun release()
}

/**
 * Thin wrapper around the vendored sherpa-onnx KeywordSpotter. It constructs
 * the spotter from the app AssetManager, keeps one stream, and resets the
 * stream after a hit. Native failures are converted to
 * [WakeWordEngineException]; no raw pointer escapes.
 */
class WakeWordEngine private constructor(
    private val spotter: KeywordSpotter,
) : WakeWordKeywordEngine {

    private val lock = Any()
    private var stream: OnlineStream? = null
    private var released = false

    override fun newStream() {
        synchronized(lock) {
            if (released) {
                throw WakeWordEngineException("Wake-word engine is already released")
            }
            stream?.release()
            stream = try {
                spotter.createStream()
            } catch (error: Throwable) {
                throw WakeWordEngineException(
                    "Could not create a keyword stream: ${reason(error)}",
                    error,
                )
            }
        }
    }

    override fun accept(samples: FloatArray, sampleRate: Int) {
        synchronized(lock) {
            if (released) {
                throw WakeWordEngineException("Wake-word engine is already released")
            }
            val current = stream
                ?: throw WakeWordEngineException("No keyword stream is active")
            try {
                current.acceptWaveform(samples, sampleRate)
            } catch (error: Throwable) {
                throw WakeWordEngineException(
                    "Could not feed audio to the keyword spotter: ${reason(error)}",
                    error,
                )
            }
        }
    }

    override fun poll(): String? {
        synchronized(lock) {
            if (released) {
                return null
            }
            val current = stream ?: return null
            return try {
                while (spotter.isReady(current)) {
                    spotter.decode(current)
                }
                val result = spotter.getResult(current)
                if (result.keyword.isNotEmpty()) {
                    spotter.reset(current)
                    result.keyword
                } else {
                    null
                }
            } catch (error: Throwable) {
                throw WakeWordEngineException(
                    "Could not read a keyword result: ${reason(error)}",
                    error,
                )
            }
        }
    }

    override fun release() {
        synchronized(lock) {
            if (released) {
                return
            }
            released = true
            try {
                stream?.release()
            } catch (_: Throwable) {
                // Releasing native resources must never fail the caller.
            }
            stream = null
            try {
                spotter.release()
            } catch (_: Throwable) {
                // Idempotent release.
            }
        }
    }

    companion object {
        const val ENCODER_FILE = "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
        const val DECODER_FILE = "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
        const val JOINER_FILE = "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
        const val TOKENS_FILE = "tokens.txt"
        const val KEYWORDS_FILE = "keywords.txt"
        const val FEATURE_DIM = 80

        /** Create the engine from the app assets or throw a typed exception. */
        fun create(context: Context, config: WakeWordConfig): WakeWordEngine {
            val errors = config.validate()
            if (errors.isNotEmpty()) {
                throw WakeWordEngineException(
                    errors.joinToString("; ") { "${it.field}: ${it.message}" },
                )
            }

            val assetDirectory = config.assetDirectory
            val assets = context.assets
            val available = try {
                assets.list(assetDirectory)?.toSet().orEmpty()
            } catch (error: Exception) {
                throw WakeWordEngineException(
                    "Could not list wake-word assets in $assetDirectory: ${reason(error)}",
                    error,
                )
            }
            val required = listOf(
                ENCODER_FILE,
                DECODER_FILE,
                JOINER_FILE,
                TOKENS_FILE,
                KEYWORDS_FILE,
            )
            val missing = required.filter { it !in available }
            if (missing.isNotEmpty()) {
                throw WakeWordEngineException(
                    "Missing wake-word assets in $assetDirectory: ${missing.joinToString(", ")}",
                )
            }

            val spotterConfig = KeywordSpotterConfig(
                featConfig = FeatureConfig(
                    sampleRate = config.captureSampleRate,
                    featureDim = FEATURE_DIM,
                ),
                modelConfig = OnlineModelConfig(
                    transducer = OnlineTransducerModelConfig(
                        encoder = "$assetDirectory/$ENCODER_FILE",
                        decoder = "$assetDirectory/$DECODER_FILE",
                        joiner = "$assetDirectory/$JOINER_FILE",
                    ),
                    tokens = "$assetDirectory/$TOKENS_FILE",
                    numThreads = config.numThreads,
                    modelType = "zipformer2",
                ),
                maxActivePaths = config.maxActivePaths,
                keywordsFile = config.keywordsAsset,
                keywordsScore = config.score,
                keywordsThreshold = config.threshold,
                numTrailingBlanks = config.trailingBlanks,
            )

            val spotter = try {
                KeywordSpotter(assets, spotterConfig)
            } catch (error: WakeWordEngineException) {
                throw error
            } catch (error: UnsatisfiedLinkError) {
                throw WakeWordEngineException(
                    "The sherpa-onnx native library is unavailable for this ABI",
                    error,
                )
            } catch (error: Throwable) {
                throw WakeWordEngineException(
                    "Could not create the keyword spotter: ${reason(error)}",
                    error,
                )
            }

            return WakeWordEngine(spotter)
        }

        private fun reason(error: Throwable): String =
            error.message?.takeIf { it.isNotBlank() } ?: error.javaClass.simpleName
    }
}
