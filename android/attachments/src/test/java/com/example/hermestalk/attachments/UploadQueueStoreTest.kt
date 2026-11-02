package com.example.hermestalk.attachments

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class UploadQueueStoreTest {

    @get:Rule
    val folder = TemporaryFolder()

    @Test
    fun roundTripsThroughANewStoreInstance() {
        val root = folder.newFolder("attachments")
        val store = UploadQueueStore(root)
        val record = record("att-1", UploadStatus.QUEUED, createdAt = 10)
        store.save(listOf(record))

        val reopened = UploadQueueStore(root)
        val loaded = reopened.load()
        assertEquals(1, loaded.size)
        assertEquals(record, loaded.first())
    }

    @Test
    fun writesAtomicallyAndLeavesNoTemporaryFiles() {
        val root = folder.newFolder("attachments")
        val store = UploadQueueStore(root)
        store.save(listOf(record("att-1", UploadStatus.QUEUED)))
        store.save(listOf(record("att-1", UploadStatus.QUEUED), record("att-2", UploadStatus.QUEUED)))

        val queueDir = File(root, "queue")
        val names = queueDir.listFiles().orEmpty().map { it.name }.sorted()
        assertEquals(listOf("index.json"), names)
        assertFalse(names.any { it.endsWith(".tmp") })
        assertEquals(2, UploadQueueStore(root).load().size)
    }

    @Test
    fun quarantinesACorruptIndexAndStartsEmpty() {
        val root = folder.newFolder("attachments")
        val queueDir = File(root, "queue")
        queueDir.mkdirs()
        File(queueDir, "index.json").writeText("{not valid json")
        val store = UploadQueueStore(root)

        assertTrue(store.load().isEmpty())
        val quarantine = queueDir.listFiles().orEmpty()
            .filter { it.name.startsWith("index.corrupt-") }
        assertEquals(1, quarantine.size)
        assertTrue(quarantine.first().readText().startsWith("{not valid json"))
        assertFalse(File(queueDir, "index.json").exists())
    }

    @Test
    fun prunesCompletedRecordsToTheNewestTwenty() {
        val root = folder.newFolder("attachments")
        val store = UploadQueueStore(root)
        val records = (1..25).map { record("sent-$it", UploadStatus.SENT, createdAt = it.toLong()) } +
            record("queued", UploadStatus.QUEUED, createdAt = 99)
        store.save(records)

        val loaded = UploadQueueStore(root).load()
        val completed = loaded.filter { UploadStatus.isCompleted(it.status) }
        assertEquals(20, completed.size)
        assertEquals(
            (6..25).map { "sent-$it" }.toSet(),
            completed.map { it.id }.toSet(),
        )
        assertEquals(1, loaded.count { it.status == UploadStatus.QUEUED })
    }

    @Test
    fun upsertReplacesAnExistingRecord() {
        val root = folder.newFolder("attachments")
        val store = UploadQueueStore(root)
        store.save(listOf(record("att-1", UploadStatus.QUEUED)))
        store.upsert(record("att-1", UploadStatus.SENT, createdAt = 10, spoken = "File saved."))
        val loaded = UploadQueueStore(root).load()
        assertEquals(1, loaded.size)
        assertEquals(UploadStatus.SENT, loaded.first().status)
        assertEquals("File saved.", loaded.first().spoken)
    }

    private fun record(
        id: String,
        status: String,
        createdAt: Long = 1,
        spoken: String? = null,
    ): UploadRecord = UploadRecord(
        id = id,
        displayName = "$id.png",
        mimeType = "image/png",
        sizeBytes = 10,
        sha256 = "abc",
        createdAtMillis = createdAt,
        status = status,
        spoken = spoken,
    )
}
