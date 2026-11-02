package com.example.hermestalk.wakeword

/** Result of parsing a sherpa-onnx keywords file. */
sealed interface WakeWordKeywordParseResult {
    /** A valid single keyword line. */
    data class Valid(
        val tokens: List<String>,
        val phrase: String,
        val label: String?,
    ) : WakeWordKeywordParseResult

    /** The file is not usable; [errors] is safe to show. */
    data class Invalid(val errors: List<String>) : WakeWordKeywordParseResult
}

/**
 * Parser and validator for the sherpa-onnx keywords file. The file must contain
 * exactly one non-empty keyword line, at least one token, no duplicate lines,
 * and the token-joined phrase must match the configured phrase. A trailing
 * `@label` is accepted only when it names the same phrase.
 */
object WakeWordKeywordFile {

    fun parse(text: String, expectedPhrase: String): WakeWordKeywordParseResult {
        val errors = mutableListOf<String>()
        val lines = text.lineSequence()
            .map { it.trim() }
            .filter { it.isNotEmpty() }
            .toList()

        if (lines.isEmpty()) {
            return WakeWordKeywordParseResult.Invalid(listOf("Keywords file is empty"))
        }
        if (lines.size > 1) {
            errors += "Keywords file must contain exactly one non-empty line"
        }
        val duplicateLines = lines.groupingBy { it }.eachCount().filterValues { it > 1 }.keys
        if (duplicateLines.isNotEmpty()) {
            errors += "Duplicate keyword lines: ${duplicateLines.joinToString(", ")}"
        }

        val line = lines.first()
        val rawTokens = line.split(Regex("\\s+")).filter { it.isNotEmpty() }
        val tokens = rawTokens.toMutableList()
        var label: String? = null
        if (tokens.isNotEmpty() && tokens.last().startsWith("@")) {
            label = tokens.removeAt(tokens.lastIndex).removePrefix("@").trim()
        }

        if (tokens.isEmpty()) {
            errors += "Keyword line has no tokens"
        }
        if (label != null && !labelsMatch(label, expectedPhrase)) {
            errors += "Trailing @label does not match the configured phrase"
        }
        if (tokens.isNotEmpty() && !tokensMatch(tokens, expectedPhrase)) {
            errors += "Keyword tokens do not match the configured phrase"
        }

        if (errors.isNotEmpty()) {
            return WakeWordKeywordParseResult.Invalid(errors)
        }
        return WakeWordKeywordParseResult.Valid(
            tokens = tokens,
            phrase = joinTokens(tokens),
            label = label,
        )
    }

    /** The token-joined form used for phrase comparison. */
    fun joinTokens(tokens: List<String>): String =
        tokens.joinToString("") { token -> token.replace("▁", "").trim() }
            .filterNot { it.isWhitespace() }
            .uppercase()

    private fun tokensMatch(tokens: List<String>, expectedPhrase: String): Boolean =
        joinTokens(tokens) == normalizePhrase(expectedPhrase)

    private fun labelsMatch(label: String, expectedPhrase: String): Boolean =
        label.filterNot { it.isWhitespace() || it == '_' }.uppercase() ==
            normalizePhrase(expectedPhrase)

    private fun normalizePhrase(phrase: String): String =
        phrase.filterNot { it.isWhitespace() }.uppercase()
}
