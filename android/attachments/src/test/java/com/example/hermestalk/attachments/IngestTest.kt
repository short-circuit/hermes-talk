package com.example.hermestalk.attachments

import java.io.ByteArrayInputStream
import java.io.File
import java.io.InputStream
import java.security.MessageDigest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class IngestTest {

    @get:Rule
    val folder = TemporaryFolder()

    @Test
    fun copiesPayloadAndComputesHash() {
        val root = folder.newFolder("attachments")
        val bytes = byteArrayOf(0x89.toByte(), 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a)
        val ingest = AttachmentIngest(root)
        val outcome = ingest.ingest(
            source = ByteArrayInputStream(bytes),
            displayName = "../Download/photo.png",
            mimeType = "image/png",
            id = "att-1",
            createdAtMillis = 1234L,
        )
        assertTrue(outcome is IngestOutcome.Success)
        val success = outcome as IngestOutcome.Success
        assertEquals("photo.png", success.record.displayName)
        assertEquals(bytes.size.toLong(), success.record.sizeBytes)
        assertEquals(bytes.size.toLong(), success.payloadFile.length())
        assertEquals(hex(bytes), success.record.sha256)
        assertTrue(success.payloadFile.readBytes().contentEquals(bytes))
        assertFalse(File(success.payloadFile.parentFile, "payload.bin.part").exists())
    }

    @Test
    fun rejectsEmptyPayload() {
        val root = folder.newFolder("attachments")
        val outcome = AttachmentIngest(root).ingest(
            source = ByteArrayInputStream(ByteArray(0)),
            displayName = "empty.txt",
            mimeType = "text/plain",
            id = "att-2",
            createdAtMillis = 1L,
        )
        assertTrue(outcome is IngestOutcome.Failure)
        assertEquals("empty_upload", (outcome as IngestOutcome.Failure).code)
        assertFalse(File(root, "queue/att-2/payload.bin").exists())
    }

    @Test
    fun rejectsPayloadOverTheCapWithoutBufferingItAll() {
        val root = folder.newFolder("attachments")
        val outcome = AttachmentIngest(root).ingest(
            source = RepeatingStream(MAX_ATTACHMENT_BYTES + 1),
            displayName = "big.bin",
            mimeType = "application/zip",
            id = "att-3",
            createdAtMillis = 1L,
        )
        assertTrue(outcome is IngestOutcome.Failure)
        assertEquals("file_too_large", (outcome as IngestOutcome.Failure).code)
        assertFalse(File(root, "queue/att-3/payload.bin").exists())
    }

    @Test
    fun addsExtensionWhenTheSharedNameHasNone() {
        val root = folder.newFolder("attachments")
        val outcome = AttachmentIngest(root).ingest(
            source = ByteArrayInputStream(byteArrayOf(1, 2, 3)),
            displayName = "attachment",
            mimeType = "image/png",
            id = "att-4",
            createdAtMillis = 1L,
        )
        assertTrue(outcome is IngestOutcome.Success)
        assertEquals("attachment.png", (outcome as IngestOutcome.Success).record.displayName)
    }

    private fun hex(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes).toHex()

    private class RepeatingStream(private val total: Long) : InputStream() {
        private var emitted = 0L

        override fun read(): Int {
            if (emitted >= total) {
                return -1
            }
            emitted += 1
            return 0x42
        }

        override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
            if (emitted >= total) {
                return -1
            }
            val count = minOf(length.toLong(), total - emitted).toInt()
            buffer.fill(0x42, offset, offset + count)
            emitted += count
            return count
        }
    }
}
