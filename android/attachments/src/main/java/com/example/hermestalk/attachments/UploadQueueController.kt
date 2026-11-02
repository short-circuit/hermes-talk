package com.example.hermestalk.attachments

import com.example.hermestalk.rtvi.RelaySettings
import java.io.File
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Single-flight sequential worker over the durable upload queue. It resumes
 * persisted work on [start], retries bounded failures with [RetryPolicy], wakes
 * immediately when connectivity returns, and deletes `payload.bin` only after a
 * 201 response. The token is never logged here.
 */
class UploadQueueController(
    private val rootDir: File,
    private val engine: UploadTransport,
    private val connectivity: ConnectivityMonitor = NoopConnectivityMonitor(),
    private val scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.IO),
    private val delayOverride: (suspend (Long) -> Unit)? = null,
    private val clock: () -> Long = { System.currentTimeMillis() },
) {

    private val store = UploadQueueStore(rootDir)
    private val ingest = AttachmentIngest(rootDir)
    private val lock = Any()
    private val records = linkedMapOf<String, UploadRecord>()

    private val _items = MutableStateFlow<List<UploadItem>>(emptyList())
    val items: StateFlow<List<UploadItem>> = _items.asStateFlow()

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    private val wakeSignal = Channel<Unit>(Channel.CONFLATED)
    private var worker: Job? = null
    private var settings: RelaySettings? = null
    private var connectivityStarted = false

    /** Load persisted work and resume it with [settings]. Safe to call repeatedly. */
    fun start(settings: RelaySettings) {
        synchronized(lock) {
            this.settings = settings
            records.clear()
            store.load().forEach { records[it.id] = it }
            publishLocked()
        }
        if (!connectivityStarted) {
            connectivityStarted = true
            connectivity.start {
                wakeSignal.trySend(Unit)
                ensureWorker()
            }
        }
        ensureWorker()
    }

    fun updateSettings(settings: RelaySettings) {
        synchronized(lock) { this.settings = settings }
    }

    /** Persist an ingested record and wake the worker. */
    fun enqueue(record: UploadRecord, settings: RelaySettings) {
        updateSettings(settings)
        synchronized(lock) {
            records[record.id] = record
            persistLocked()
        }
        _error.value = null
        ensureWorker()
    }

    /** Copy [source] into the queue, then enqueue it. Runs on the caller's dispatcher. */
    fun ingestAndEnqueue(
        source: java.io.InputStream,
        displayName: String?,
        mimeType: String?,
        settings: RelaySettings,
    ): UploadItem? {
        updateSettings(settings)
        val id = newId()
        return when (
            val outcome = ingest.ingest(
                source = source,
                displayName = displayName,
                mimeType = mimeType,
                id = id,
                createdAtMillis = clock(),
            )
        ) {
            is IngestOutcome.Success -> {
                enqueue(outcome.record, settings)
                outcome.record.toItem()
            }

            is IngestOutcome.Failure -> {
                _error.value = outcome.spoken
                null
            }
        }
    }

    /** Reset a failed or cancelled record to queued and wake the worker. */
    fun retry(id: String, settings: RelaySettings) {
        updateSettings(settings)
        val payload = store.payloadFile(id)
        synchronized(lock) {
            val record = records[id] ?: return
            val updated = if (!payload.exists()) {
                record.copy(
                    status = UploadStatus.FAILED,
                    failureCode = "payload_missing",
                    failureSpoken = "The saved file is missing.",
                )
            } else {
                record.copy(
                    status = UploadStatus.QUEUED,
                    attempts = 0,
                    spoken = null,
                    failureCode = null,
                    failureSpoken = null,
                )
            }
            records[id] = updated
            persistLocked()
        }
        _error.value = null
        ensureWorker()
    }

    /** Cancel an upload and remove its payload. */
    fun cancel(id: String) {
        synchronized(lock) {
            val record = records[id] ?: return
            records[id] = record.copy(
                status = UploadStatus.CANCELLED,
                failureCode = null,
                failureSpoken = null,
            )
            persistLocked()
        }
        store.payloadFile(id).delete()
        File(rootDir, "queue/$id").deleteRecursively()
        ensureWorker()
    }

    /** Stop the connectivity callback and cancel the worker. */
    fun close() {
        connectivity.stop()
        connectivityStarted = false
        worker?.cancel()
        worker = null
    }

    private fun ensureWorker() {
        synchronized(lock) {
            if (worker?.isActive == true) {
                wakeSignal.trySend(Unit)
                return
            }
            worker = scope.launch { runQueue() }
        }
    }

    private suspend fun runQueue() {
        try {
            while (currentCoroutineContext().isActive) {
                val next = synchronized(lock) {
                    records.values.firstOrNull {
                        UploadStatus.isResumable(it.status)
                    }
                } ?: break
                uploadOne(next.id)
            }
        } catch (cancelled: CancellationException) {
            throw cancelled
        }
    }

    private suspend fun uploadOne(id: String) {
        val currentSettings = synchronized(lock) { settings } ?: return
        val record = synchronized(lock) { records[id] } ?: return
        val payloadFile = store.payloadFile(id)
        if (!payloadFile.exists()) {
            markFailed(
                id,
                code = "payload_missing",
                spoken = "The saved file is missing.",
            )
            return
        }

        val payload = UploadPayload(
            file = payloadFile,
            displayName = record.displayName,
            mimeType = record.mimeType,
            sizeBytes = record.sizeBytes,
            sha256 = record.sha256,
        )
        updateState(id) { it.copy(state = UploadItemState.Uploading(0f)) }

        val runner = UploadRunner(engine) { millis -> waitForRetry(millis) }
        val result = try {
            runner.run(
                settings = currentSettings,
                payload = payload,
                onProgress = { uploaded, total ->
                    val fraction = if (total <= 0L) 0f else {
                        (uploaded.toDouble() / total.toDouble()).coerceIn(0.0, 1.0).toFloat()
                    }
                    updateState(id) { it.copy(state = UploadItemState.Uploading(fraction)) }
                },
                onAttempt = { attempt ->
                    synchronized(lock) {
                        val existing = records[id] ?: return@synchronized
                        records[id] = existing.copy(
                            status = UploadStatus.UPLOADING,
                            attempts = attempt,
                        )
                        persistLocked()
                    }
                },
                onRetry = { nextAttempt, delaySeconds ->
                    synchronized(lock) {
                        val existing = records[id] ?: return@synchronized
                        records[id] = existing.copy(
                            status = UploadStatus.RETRYING,
                            attempts = nextAttempt - 1,
                        )
                        persistLocked()
                    }
                    updateState(id) {
                        it.copy(state = UploadItemState.Retrying(nextAttempt, delaySeconds))
                    }
                },
            )
        } catch (cancelled: CancellationException) {
            throw cancelled
        }

        when (val outcome = result.outcome) {
            is UploadOutcome.Success -> {
                synchronized(lock) {
                    val existing = records[id] ?: return@synchronized
                    records[id] = existing.copy(
                        status = UploadStatus.SENT,
                        attempts = result.attempts,
                        spoken = outcome.result.spoken,
                        failureCode = null,
                        failureSpoken = null,
                    )
                    persistLocked()
                }
                payloadFile.delete()
                File(rootDir, "queue/$id").deleteRecursively()
                updateState(id) {
                    it.copy(state = UploadItemState.Sent(outcome.result.spoken))
                }
            }

            is UploadOutcome.Failure -> {
                markFailed(
                    id,
                    code = outcome.failure.code,
                    spoken = outcome.failure.spoken,
                    attempts = result.attempts,
                )
                _error.value = outcome.failure.spoken
            }
        }
    }

    private suspend fun waitForRetry(millis: Long) {
        val override = delayOverride
        if (override != null) {
            override(millis)
            return
        }
        withTimeoutOrNull(millis) {
            wakeSignal.receive()
        }
    }

    private fun markFailed(
        id: String,
        code: String,
        spoken: String,
        attempts: Int? = null,
    ) {
        synchronized(lock) {
            val existing = records[id] ?: return
            records[id] = existing.copy(
                status = UploadStatus.FAILED,
                attempts = attempts ?: existing.attempts,
                failureCode = code,
                failureSpoken = spoken,
            )
            persistLocked()
        }
        updateState(id) { it.copy(state = UploadItemState.Failed(spoken, code)) }
    }

    private fun updateState(id: String, transform: (UploadItem) -> UploadItem) {
        _items.update { list ->
            list.map { if (it.id == id) transform(it) else it }
        }
    }

    private fun publishLocked() {
        _items.value = records.values.map { it.toItem() }
    }

    private fun persistLocked() {
        val saved = store.save(records.values.toList())
        records.clear()
        saved.forEach { records[it.id] = it }
        publishLocked()
    }

    private fun newId(): String = "att-" + java.util.UUID.randomUUID().toString().replace("-", "")
}
