package com.example.hermestalk.attachments

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AttachmentNamesTest {

    @Test
    fun stripsPosixAndWindowsPaths() {
        assertEquals("photo.png", AttachmentNames.sanitize("/sdcard/Download/photo.png"))
        assertEquals("photo.png", AttachmentNames.sanitize("C:\\Users\\me\\photo.png"))
    }

    @Test
    fun replacesControlCharacters() {
        val sanitized = AttachmentNames.sanitize("a\u0000b\u001fc.png")
        assertEquals("a_b_c.png", sanitized)
    }

    @Test
    fun capsLengthAndFallsBack() {
        val longName = "x".repeat(500) + ".png"
        assertTrue(AttachmentNames.sanitize(longName).length <= AttachmentNames.MAX_LENGTH)
        assertEquals("attachment", AttachmentNames.sanitize("   "))
        assertEquals("attachment", AttachmentNames.sanitize(null))
        assertEquals("attachment", AttachmentNames.sanitize("..."))
    }

    @Test
    fun addsExtensionFromMimeWhenMissing() {
        assertEquals("attachment.png", AttachmentNames.withExtension("attachment", "image/png"))
        assertEquals("photo.jpg", AttachmentNames.withExtension("photo", "image/jpeg"))
        assertEquals("report.pdf", AttachmentNames.withExtension("report", "application/pdf"))
        assertEquals("photo.png", AttachmentNames.withExtension("photo.png", "image/jpeg"))
        assertEquals("attachment", AttachmentNames.withExtension("attachment", "application/octet-stream"))
    }
}
