package com.example.hermestalk.attachments

import java.io.File
import java.io.FileOutputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.util.UUID
import kotlinx.serialization.Serializable
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json

@Serializable
internal data class QueueIndex(
    val version: Int = 1,
    val records: List<UploadRecord> = emptyList(),
)

/**
 * Durable JSON index for the attachment queue. Writes go to a temporary file in
 * the same directory and are moved into place atomically, so a crash cannot
 * leave a half-written index. A corrupt index is quarantined as
 * `index.corrupt-<timestamp>.json` and replaced by an empty queue instead of
 * crashing the app. Completed records are pruned to the newest [KEEP_COMPLETED].
 */
class UploadQueueStore(private val rootDir: File) {

    private val queueDir = File(rootDir, "queue")
    private val indexFile = File(queueDir, "index.json")
    private val lock = Any()

    private val json = Json {
        ignoreUnknownKeys = true
        encodeDefaults = true
        prettyPrint = false
    }

    fun payloadFile(id: String): File = File(queueDir, "$id/payload.bin")

    /** Load the queue. A missing index yields an empty queue. */
    fun load(): List<UploadRecord> = synchronized(lock) {
        readIndex()
    }

    /** Persist [records] after pruning completed entries. Returns what was written. */
    fun save(records: List<UploadRecord>): List<UploadRecord> = synchronized(lock) {
        val pruned = pruneCompleted(records)
        writeIndex(pruned)
        pruned
    }

    /** Insert or replace one record. */
    fun upsert(record: UploadRecord): List<UploadRecord> = synchronized(lock) {
        val current = readIndex().toMutableList()
        val index = current.indexOfFirst { it.id == record.id }
        if (index >= 0) {
            current[index] = record
        } else {
            current += record
        }
        val pruned = pruneCompleted(current)
        writeIndex(pruned)
        pruned
    }

    /** Remove one record and its payload directory. */
    fun remove(id: String): List<UploadRecord> = synchronized(lock) {
        val pruned = pruneCompleted(readIndex().filterNot { it.id == id })
        writeIndex(pruned)
        File(queueDir, id).deleteRecursively()
        pruned
    }

    /** Keep every unfinished record plus the newest [KEEP_COMPLETED] completed ones. */
    fun pruneCompleted(
        records: List<UploadRecord>,
        keep: Int = KEEP_COMPLETED,
    ): List<UploadRecord> {
        val completed = records.filter { UploadStatus.isCompleted(it.status) }
        val keepIds = completed
            .sortedByDescending { it.createdAtMillis }
            .take(keep)
            .mapTo(hashSetOf()) { it.id }
        return records.filter { record ->
            !UploadStatus.isCompleted(record.status) || record.id in keepIds
        }
    }

    private fun readIndex(): List<UploadRecord> {
        if (!indexFile.exists()) {
            return emptyList()
        }
        return try {
            val text = indexFile.readText()
            if (text.isBlank()) {
                return emptyList()
            }
            json.decodeFromString<QueueIndex>(text).records
        } catch (failure: Exception) {
            quarantine()
            emptyList()
        }
    }

    private fun writeIndex(records: List<UploadRecord>) {
        if (!queueDir.exists() && !queueDir.mkdirs()) {
            return
        }
        val temp = File(queueDir, ".index-${UUID.randomUUID()}.tmp")
        try {
            val bytes = json.encodeToString(QueueIndex.serializer(), QueueIndex(records = records))
                .toByteArray(Charsets.UTF_8)
            FileOutputStream(temp).use { output ->
                output.write(bytes)
                output.fd.sync()
            }
            try {
                Files.move(
                    temp.toPath(),
                    indexFile.toPath(),
                    StandardCopyOption.ATOMIC_MOVE,
                    StandardCopyOption.REPLACE_EXISTING,
                )
            } catch (_: Exception) {
                Files.move(
                    temp.toPath(),
                    indexFile.toPath(),
                    StandardCopyOption.REPLACE_EXISTING,
                )
            }
        } catch (_: Exception) {
            temp.delete()
        } finally {
            if (temp.exists()) {
                temp.delete()
            }
        }
    }

    private fun quarantine() {
        val stamp = System.currentTimeMillis()
        var target = File(queueDir, "index.corrupt-$stamp.json")
        var suffix = 0
        while (target.exists()) {
            suffix += 1
            target = File(queueDir, "index.corrupt-$stamp-$suffix.json")
        }
        runCatching { indexFile.renameTo(target) }
            .onFailure { indexFile.copyTo(target, overwrite = true); indexFile.delete() }
    }

    companion object {
        const val KEEP_COMPLETED = 20
    }
}
