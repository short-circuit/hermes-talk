package com.example.hermestalk.wakeword

import android.content.res.AssetManager
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.charset.StandardCharsets

/** A decoded 16 kHz mono PCM16 WAV fixture. */
data class WavFixture(val samples: FloatArray, val sampleRate: Int)

/** Normalize a keyword result or token line for comparison. */
fun normalizeKeyword(value: String): String =
    value.filterNot { it.isWhitespace() || it == '▁' || it == '_' }.uppercase()

/**
 * Read a PCM16 WAV fixture from the instrumentation asset APK. The fixtures
 * are generated at 16 kHz mono; other layouts fail loudly.
 */
fun readWavFixture(assets: AssetManager, path: String): WavFixture {
    val bytes = assets.open(path).use { it.readBytes() }
    require(bytes.size >= 12) { "$path is too short to be a WAV file" }
    require(String(bytes, 0, 4, StandardCharsets.US_ASCII) == "RIFF") {
        "$path is not a RIFF file"
    }
    require(String(bytes, 8, 4, StandardCharsets.US_ASCII) == "WAVE") {
        "$path is not a WAVE file"
    }

    var position = 12
    var sampleRate = 0
    var channels = 0
    var bitsPerSample = 0
    var dataOffset = -1
    var dataSize = 0
    var foundFormat = false

    while (position + 8 <= bytes.size) {
        val id = String(bytes, position, 4, StandardCharsets.US_ASCII)
        val size = littleEndianInt(bytes, position + 4)
        val payload = position + 8
        if (size < 0 || payload > bytes.size) {
            break
        }
        when (id) {
            "fmt " -> {
                if (payload + 16 <= bytes.size) {
                    val format = littleEndianShort(bytes, payload)
                    channels = littleEndianShort(bytes, payload + 2)
                    sampleRate = littleEndianInt(bytes, payload + 4)
                    bitsPerSample = littleEndianShort(bytes, payload + 14)
                    require(format == 1) { "$path is not PCM" }
                    foundFormat = true
                }
            }

            "data" -> {
                dataOffset = payload
                dataSize = minOf(size, bytes.size - payload)
            }
        }
        position = payload + size + (size and 1)
    }

    require(foundFormat) { "$path has no fmt chunk" }
    require(dataOffset >= 0 && dataSize > 0) { "$path has no data chunk" }
    require(channels == 1) { "$path must be mono" }
    require(bitsPerSample == 16) { "$path must be PCM16" }
    require(sampleRate == 16_000) { "$path must be 16 kHz" }

    val frameCount = dataSize / 2
    val buffer = ByteBuffer.wrap(bytes, dataOffset, dataSize).order(ByteOrder.LITTLE_ENDIAN)
    val samples = FloatArray(frameCount) {
        buffer.short / 32768.0f
    }
    return WavFixture(samples = samples, sampleRate = sampleRate)
}

private fun littleEndianInt(bytes: ByteArray, offset: Int): Int =
    (bytes[offset].toInt() and 0xff) or
        ((bytes[offset + 1].toInt() and 0xff) shl 8) or
        ((bytes[offset + 2].toInt() and 0xff) shl 16) or
        ((bytes[offset + 3].toInt() and 0xff) shl 24)

private fun littleEndianShort(bytes: ByteArray, offset: Int): Int =
    (bytes[offset].toInt() and 0xff) or
        ((bytes[offset + 1].toInt() and 0xff) shl 8)
