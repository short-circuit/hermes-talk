package com.example.hermestalk.attachments

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import androidx.core.content.FileProvider
import java.io.File

/**
 * Opens a downloaded artifact through [FileProvider]. The URI grant is scoped to
 * the viewer activity; no broad file exposure is needed. Returns false when no
 * installed app can handle the MIME type.
 */
class ArtifactOpener {

    fun open(context: Context, file: File, mimeType: String, authority: String): Boolean {
        if (!file.exists()) {
            return false
        }
        val uri = try {
            FileProvider.getUriForFile(context, authority, file)
        } catch (_: IllegalArgumentException) {
            return false
        }
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, mimeType.ifBlank { "application/octet-stream" })
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            if (context !is Activity) {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
        }
        return try {
            context.startActivity(intent)
            true
        } catch (_: ActivityNotFoundException) {
            false
        }
    }
}
