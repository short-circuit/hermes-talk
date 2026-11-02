package com.example.hermestalk.attachments

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ShareIntentParserTest {

    @Test
    fun parsesSingleShareAndCollapsesDuplicates() {
        val files = ShareIntentParser.parse(
            ShareExtras(
                action = "android.intent.action.SEND",
                mimeType = "image/png",
                single = ShareUri("content://media/1"),
                clipItems = listOf(ShareUri("content://media/1")),
                text = "content://media/2",
            ),
        )
        assertEquals(2, files.size)
        assertEquals("content://media/1", files[0].uriString)
        assertEquals("content://media/2", files[1].uriString)
        assertEquals("image/png", files[0].mimeType)
    }

    @Test
    fun parsesMultipleShareWithLabels() {
        val files = ShareIntentParser.parse(
            ShareExtras(
                action = "android.intent.action.SEND_MULTIPLE",
                mimeType = "application/pdf",
                multiple = listOf(ShareUri("content://media/3")),
                clipItems = listOf(ShareUri("content://media/4", "report.pdf")),
            ),
        )
        assertEquals(listOf("content://media/3", "content://media/4"), files.map { it.uriString })
        assertEquals("report.pdf", files[1].displayName)
    }

    @Test
    fun ignoresBlankAndNullUris() {
        val files = ShareIntentParser.parse(
            ShareExtras(
                action = "android.intent.action.SEND_MULTIPLE",
                multiple = listOf(ShareUri(null), ShareUri("  "), ShareUri("content://media/5")),
                clipItems = listOf(ShareUri("")),
            ),
        )
        assertEquals(listOf("content://media/5"), files.map { it.uriString })
    }

    @Test
    fun ignoresNonShareActions() {
        val files = ShareIntentParser.parse(
            ShareExtras(
                action = "android.intent.action.VIEW",
                single = ShareUri("content://media/6"),
            ),
        )
        assertTrue(files.isEmpty())
    }

    @Test
    fun ignoresPlainTextInExtraText() {
        val files = ShareIntentParser.parse(
            ShareExtras(
                action = "android.intent.action.SEND",
                text = "just a sentence",
            ),
        )
        assertTrue(files.isEmpty())
    }
}
