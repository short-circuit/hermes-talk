package com.example.hermestalk.attachments.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.example.hermestalk.attachments.ArtifactDownloadState
import com.example.hermestalk.attachments.ArtifactUiState
import com.example.hermestalk.attachments.R
import com.example.hermestalk.attachments.UploadItem
import com.example.hermestalk.attachments.UploadItemState

/**
 * Stateless attachment panel: the in-call picker, per-file upload state and the
 * relay artifact list. All actions are delegated so the panel is previewable
 * and testable without a controller.
 */
@Composable
fun AttachmentPanel(
    uploads: List<UploadItem>,
    artifactState: ArtifactUiState,
    uploadError: String?,
    onPick: () -> Unit,
    onRetryUpload: (String) -> Unit,
    onCancelUpload: (String) -> Unit,
    onRefreshArtifacts: () -> Unit,
    onDownloadArtifact: (String) -> Unit,
    onOpenArtifact: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    Card(modifier = modifier.fillMaxWidth()) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Text(
                text = stringResource(R.string.attachment_title),
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold,
            )
            Button(
                onClick = onPick,
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag("attachment_pick"),
            ) {
                Text(stringResource(R.string.attachment_pick))
            }

            if (uploadError != null) {
                Text(
                    text = stringResource(R.string.attachment_error, uploadError),
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.testTag("attachment_error"),
                )
            }

            if (uploads.isEmpty()) {
                Text(
                    text = stringResource(R.string.attachment_empty),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            } else {
                // Newest first: a just-shared file must be visible without
                // scrolling past the history.
                for (item in uploads.asReversed()) {
                    UploadRow(
                        item = item,
                        onRetry = { onRetryUpload(item.id) },
                        onCancel = { onCancelUpload(item.id) },
                    )
                }
            }

            Text(
                text = stringResource(R.string.artifact_title),
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold,
            )
            Button(
                onClick = onRefreshArtifacts,
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag("artifact_refresh"),
                enabled = !artifactState.loading,
            ) {
                Text(stringResource(R.string.artifact_refresh))
            }
            if (artifactState.error != null) {
                Text(
                    text = stringResource(R.string.attachment_error, artifactState.error),
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.testTag("attachment_error"),
                )
            }
            if (artifactState.artifacts.isEmpty()) {
                Text(
                    text = stringResource(R.string.artifact_empty),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            } else {
                for (artifact in artifactState.artifacts) {
                    ArtifactRow(
                        artifactId = artifact.id,
                        name = artifact.name,
                        mime = artifact.mime,
                        sizeBytes = artifact.sizeBytes,
                        download = artifactState.downloads[artifact.id],
                        onDownload = { onDownloadArtifact(artifact.id) },
                        onOpen = { onOpenArtifact(artifact.id) },
                    )
                }
            }
        }
    }
}

@Composable
private fun UploadRow(
    item: UploadItem,
    onRetry: () -> Unit,
    onCancel: () -> Unit,
) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .testTag("attachment_upload_item"),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceVariant,
        ),
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            Text(
                text = item.displayName,
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.SemiBold,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Text(
                text = stringResource(R.string.attachment_size, item.sizeBytes),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            when (val state = item.state) {
                UploadItemState.Queued -> Text(stringResource(R.string.attachment_queued))
                is UploadItemState.Uploading -> {
                    Text(stringResource(R.string.attachment_uploading))
                    LinearProgressIndicator(
                        progress = { state.progress },
                        modifier = Modifier
                            .fillMaxWidth()
                            .testTag("attachment_progress"),
                    )
                }

                is UploadItemState.Retrying -> {
                    Text(
                        stringResource(
                            R.string.attachment_retrying,
                            state.delaySeconds,
                            state.attempt,
                        ),
                    )
                    LinearProgressIndicator(
                        modifier = Modifier
                            .fillMaxWidth()
                            .testTag("attachment_progress"),
                    )
                }

                is UploadItemState.Sent -> Text(stringResource(R.string.attachment_sent, state.spoken))
                is UploadItemState.Failed ->
                    Text(
                        text = stringResource(R.string.attachment_failed, state.spoken),
                        color = MaterialTheme.colorScheme.error,
                    )

                UploadItemState.Cancelled -> Text(stringResource(R.string.attachment_cancelled))
            }
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.End,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                if (
                    item.state is UploadItemState.Failed ||
                    item.state is UploadItemState.Cancelled
                ) {
                    TextButton(
                        onClick = onRetry,
                        modifier = Modifier.testTag("attachment_retry"),
                    ) {
                        Text(stringResource(R.string.attachment_retry))
                    }
                }
                if (
                    item.state is UploadItemState.Queued ||
                    item.state is UploadItemState.Uploading ||
                    item.state is UploadItemState.Retrying ||
                    item.state is UploadItemState.Failed
                ) {
                    TextButton(
                        onClick = onCancel,
                        modifier = Modifier.testTag("attachment_cancel"),
                    ) {
                        Text(stringResource(R.string.attachment_cancel))
                    }
                }
            }
        }
    }
}

@Composable
private fun ArtifactRow(
    artifactId: String,
    name: String,
    mime: String,
    sizeBytes: Long,
    download: ArtifactDownloadState?,
    onDownload: () -> Unit,
    onOpen: () -> Unit,
) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .testTag("artifact_item"),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceVariant,
        ),
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            Text(
                text = name,
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.SemiBold,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Text(
                text = stringResource(R.string.artifact_item_detail, mime, sizeBytes),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            when (download) {
                null -> Button(
                    onClick = onDownload,
                    modifier = Modifier.testTag("artifact_download"),
                ) {
                    Text(stringResource(R.string.artifact_download))
                }

                is ArtifactDownloadState.Downloading -> {
                    Text(stringResource(R.string.artifact_downloading))
                    LinearProgressIndicator(
                        progress = { download.progress },
                        modifier = Modifier
                            .fillMaxWidth()
                            .testTag("attachment_progress"),
                    )
                }

                is ArtifactDownloadState.Downloaded -> {
                    Text(
                        text = stringResource(R.string.artifact_downloaded),
                        color = MaterialTheme.colorScheme.primary,
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Button(
                        onClick = onOpen,
                        modifier = Modifier.testTag("artifact_open"),
                    ) {
                        Text(stringResource(R.string.artifact_open))
                    }
                }

                is ArtifactDownloadState.Failed -> {
                    Text(
                        text = stringResource(
                            R.string.artifact_download_failed,
                            download.spoken,
                        ),
                        color = MaterialTheme.colorScheme.error,
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Button(
                        onClick = onDownload,
                        modifier = Modifier.testTag("artifact_download"),
                    ) {
                        Text(stringResource(R.string.artifact_download))
                    }
                }
            }
            Spacer(Modifier.width(0.dp))
            Text(
                text = artifactId,
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        }
    }
}
