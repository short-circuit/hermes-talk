package com.example.hermestalk.attachments

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.OpenableColumns
import com.example.hermestalk.rtvi.RelaySettings
import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** Per-artifact download state shown in the panel. */
sealed interface ArtifactDownloadState {
    data class Downloading(val progress: Float) : ArtifactDownloadState

    data class Downloaded(val path: String, val mime: String) : ArtifactDownloadState

    data class Failed(val spoken: String) : ArtifactDownloadState
}

/** Artifact list and download state for the UI. */
data class ArtifactUiState(
    val artifacts: List<ArtifactInfo> = emptyList(),
    val loading: Boolean = false,
    val error: String? = null,
    val retentionDays: Int? = null,
    val downloads: Map<String, ArtifactDownloadState> = emptyMap(),
)

/**
 * Process-wide owner of the durable upload queue and artifact client. Every
 * operation receives the current [RelaySettings]; the token is only used for
 * the Authorization header and is never logged.
 */
class AttachmentsController(
    context: Context,
    private val scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.IO),
) {

    private val appContext = context.applicationContext
    private val rootDir = File(appContext.filesDir, "attachments")
    private val queueController = UploadQueueController(
        rootDir = rootDir,
        engine = HttpUploadEngine(),
        connectivity = AndroidConnectivityMonitor(appContext),
        scope = scope,
    )
    private val artifactClient = ArtifactClient(File(appContext.cacheDir, "artifacts"))
    private val opener = ArtifactOpener()
    private val authority = appContext.packageName + ".fileprovider"

    private val _artifactState = MutableStateFlow(ArtifactUiState())
    val artifactState: StateFlow<ArtifactUiState> = _artifactState.asStateFlow()

    val uploads: StateFlow<List<UploadItem>> = queueController.items
    val uploadError: StateFlow<String?> = queueController.error

    private var started = false

    /** Resume persisted uploads and apply the current settings. */
    fun start(settings: RelaySettings) {
        started = true
        queueController.start(settings)
    }

    /** Apply new settings without reloading the persisted queue. */
    fun updateSettings(settings: RelaySettings) {
        queueController.updateSettings(settings)
    }

    /** Parse an ACTION_SEND / ACTION_SEND_MULTIPLE intent and queue every file. */
    fun attachFromIntent(intent: Intent, settings: RelaySettings) {
        ensureStarted(settings)
        _artifactState.update { it.copy(error = null) }
        val files = ShareIntent.read(intent)
        if (files.isEmpty()) {
            _artifactState.update { it.copy(error = "No file was shared.") }
            return
        }
        for (file in files) {
            enqueueUri(Uri.parse(file.uriString), file.displayName, file.mimeType, settings)
        }
    }

    /** Queue one document-picker URI. */
    fun pickAttachment(uri: Uri, settings: RelaySettings) {
        ensureStarted(settings)
        _artifactState.update { it.copy(error = null) }
        runCatching {
            appContext.contentResolver.takePersistableUriPermission(
                uri,
                Intent.FLAG_GRANT_READ_URI_PERMISSION,
            )
        }
        enqueueUri(uri, null, null, settings)
    }

    /** Retry a failed or cancelled upload. */
    fun retryUpload(id: String, settings: RelaySettings) {
        queueController.retry(id, settings)
    }

    /** Cancel an upload and delete its persisted payload. */
    fun cancelUpload(id: String) {
        queueController.cancel(id)
    }

    /** Refresh the relay artifact list. */
    fun refreshArtifacts(settings: RelaySettings) {
        _artifactState.update { it.copy(loading = true, error = null) }
        scope.launch {
            when (val outcome = artifactClient.list(settings)) {
                is ArtifactListOutcome.Success -> _artifactState.update {
                    it.copy(
                        artifacts = outcome.list.artifacts,
                        retentionDays = outcome.list.retentionDays,
                        loading = false,
                        error = null,
                    )
                }

                is ArtifactListOutcome.Failure -> _artifactState.update {
                    it.copy(loading = false, error = outcome.error.spoken)
                }
            }
        }
    }

    /** Download and verify one artifact into the app cache. */
    fun downloadArtifact(id: String, settings: RelaySettings) {
        val artifact = _artifactState.value.artifacts.firstOrNull { it.id == id } ?: return
        _artifactState.update {
            it.copy(
                downloads = it.downloads + (id to ArtifactDownloadState.Downloading(0f)),
            )
        }
        scope.launch {
            val outcome = artifactClient.download(settings, artifact) { downloaded, total ->
                val fraction = if (total <= 0L) 0f else {
                    (downloaded.toDouble() / total.toDouble()).coerceIn(0.0, 1.0).toFloat()
                }
                _artifactState.update {
                    it.copy(
                        downloads = it.downloads +
                            (id to ArtifactDownloadState.Downloading(fraction)),
                    )
                }
            }
            when (outcome) {
                is ArtifactDownloadOutcome.Success -> _artifactState.update {
                    it.copy(
                        downloads = it.downloads + (
                            id to ArtifactDownloadState.Downloaded(
                                outcome.file.absolutePath,
                                outcome.mime,
                            )
                            ),
                    )
                }

                is ArtifactDownloadOutcome.Failure -> _artifactState.update {
                    it.copy(
                        downloads = it.downloads +
                            (id to ArtifactDownloadState.Failed(outcome.error.spoken)),
                    )
                }
            }
        }
    }

    /** Open a previously downloaded artifact in an installed viewer. */
    fun openArtifact(id: String) {
        val downloaded = _artifactState.value.downloads[id] as? ArtifactDownloadState.Downloaded
            ?: return
        val opened = opener.open(
            appContext,
            File(downloaded.path),
            downloaded.mime,
            authority,
        )
        if (!opened) {
            _artifactState.update {
                it.copy(error = "No app can open this file.")
            }
        }
    }

    /** Stop the connectivity callback; used by tests and process teardown. */
    fun close() {
        queueController.close()
    }

    private fun ensureStarted(settings: RelaySettings) {
        if (started) {
            queueController.updateSettings(settings)
        } else {
            start(settings)
        }
    }

    private fun enqueueUri(
        uri: Uri,
        displayName: String?,
        mimeType: String?,
        settings: RelaySettings,
    ) {
        scope.launch {
            val resolvedName = displayName?.takeIf { it.isNotBlank() } ?: queryDisplayName(uri)
            val resolvedMime = mimeType?.takeIf { it.isNotBlank() }
                ?: runCatching { appContext.contentResolver.getType(uri) }.getOrNull()
            val stream = withContext(Dispatchers.IO) {
                runCatching { appContext.contentResolver.openInputStream(uri) }.getOrNull()
            }
            if (stream == null) {
                _artifactState.update { it.copy(error = "The phone could not read that file.") }
                return@launch
            }
            val item = withContext(Dispatchers.IO) {
                queueController.ingestAndEnqueue(
                    source = stream,
                    displayName = resolvedName,
                    mimeType = resolvedMime,
                    settings = settings,
                )
            }
            if (item != null) {
                _artifactState.update { it.copy(error = null) }
            }
        }
    }

    private fun queryDisplayName(uri: Uri): String? {
        val projection = arrayOf(OpenableColumns.DISPLAY_NAME)
        return runCatching {
            appContext.contentResolver.query(uri, projection, null, null, null)?.use { cursor ->
                if (!cursor.moveToFirst()) {
                    return@use null
                }
                val index = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (index < 0) null else cursor.getString(index)
            }
        }.getOrNull() ?: uri.lastPathSegment
    }
}

/** Process-wide singleton, mirroring the call controller's access pattern. */
object Attachments {

    @Volatile
    private var instance: AttachmentsController? = null

    fun get(context: Context): AttachmentsController {
        val existing = instance
        if (existing != null) {
            return existing
        }
        return synchronized(this) {
            val current = instance
            if (current != null) {
                current
            } else {
                AttachmentsController(context.applicationContext).also { instance = it }
            }
        }
    }
}
