package com.example.hermestalk.wakeword

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class WakeWordKeywordFileTest {

    @Test
    fun bundledStyleLineParses() {
        val result = WakeWordKeywordFile.parse("▁HE Y ▁HER ME S\n", "HEY HERMES")
        assertTrue(result is WakeWordKeywordParseResult.Valid)
        val valid = result as WakeWordKeywordParseResult.Valid
        assertEquals("HEYHERMES", valid.phrase)
        assertEquals(null, valid.label)
        assertEquals(listOf("▁HE", "Y", "▁HER", "ME", "S"), valid.tokens)
    }

    @Test
    fun matchingTrailingLabelIsAccepted() {
        val result = WakeWordKeywordFile.parse("▁HE Y ▁HER ME S @HEY_HERMES", "HEY HERMES")
        assertTrue(result is WakeWordKeywordParseResult.Valid)
        assertEquals("HEY_HERMES", (result as WakeWordKeywordParseResult.Valid).label)
    }

    @Test
    fun emptyFileIsRejected() {
        val result = WakeWordKeywordFile.parse("\n \n", "HEY HERMES")
        assertTrue(result is WakeWordKeywordParseResult.Invalid)
        assertEquals(listOf("Keywords file is empty"), (result as WakeWordKeywordParseResult.Invalid).errors)
    }

    @Test
    fun multipleNonEmptyLinesAreRejected() {
        val result = WakeWordKeywordFile.parse("▁HE Y ▁HER ME S\n▁HE Y ▁HER ME S\n", "HEY HERMES")
        assertTrue(result is WakeWordKeywordParseResult.Invalid)
        val errors = (result as WakeWordKeywordParseResult.Invalid).errors
        assertTrue(errors.any { it.contains("exactly one") })
        assertTrue(errors.any { it.contains("Duplicate") })
    }

    @Test
    fun mismatchedLabelIsRejected() {
        val result = WakeWordKeywordFile.parse("▁HE Y ▁HER ME S @HELLO", "HEY HERMES")
        assertTrue(result is WakeWordKeywordParseResult.Invalid)
        assertTrue(
            (result as WakeWordKeywordParseResult.Invalid).errors.any { it.contains("@label") },
        )
    }

    @Test
    fun mismatchedTokensAreRejected() {
        val result = WakeWordKeywordFile.parse("▁HE Y ▁OTHER", "HEY HERMES")
        assertTrue(result is WakeWordKeywordParseResult.Invalid)
        assertTrue(
            (result as WakeWordKeywordParseResult.Invalid).errors.any { it.contains("tokens") },
        )
    }

    @Test
    fun labelOnlyLineHasNoTokens() {
        val result = WakeWordKeywordFile.parse("@HEY_HERMES", "HEY HERMES")
        assertTrue(result is WakeWordKeywordParseResult.Invalid)
        assertTrue(
            (result as WakeWordKeywordParseResult.Invalid).errors.any { it.contains("no tokens") },
        )
    }
}
