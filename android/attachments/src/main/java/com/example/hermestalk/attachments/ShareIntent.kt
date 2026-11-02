package com.example.hermestalk.attachments

import android.content.Intent
import android.net.Uri

/** One file offered by a share intent. */
data class SharedFile(
    val uriString: String,
    val displayName: String? = null,
    val mimeType: String? = null,
)

/** One URI plus the optional label carried by a clip item. */
data class ShareUri(
    val uriString: String?,
    val label: String? = null,
)

/**
 * Framework-free representation of the extras that matter for sharing. The
 * Android [Intent] wrapper below fills this in; tests construct it directly so
 * they never need [android.net.Uri].
 */
data class ShareExtras(
    val action: String? = null,
    val mimeType: String? = null,
    val single: ShareUri? = null,
    val multiple: List<ShareUri> = emptyList(),
    val clipItems: List<ShareUri> = emptyList(),
    val text: String? = null,
)

/**
 * Pure parsing rules for ACTION_SEND and ACTION_SEND_MULTIPLE. Null or blank
 * URIs are ignored, duplicates are collapsed in first-seen order, and no I/O is
 * performed.
 */
object ShareIntentParser {

    private const val ACTION_SEND = "android.intent.action.SEND"
    private const val ACTION_SEND_MULTIPLE = "android.intent.action.SEND_MULTIPLE"

    fun parse(extras: ShareExtras): List<SharedFile> {
        val candidates = when (extras.action) {
            ACTION_SEND -> buildList {
                extras.single?.let(::add)
                extras.clipItems.forEach(::add)
                textUri(extras.text)?.let { add(ShareUri(it)) }
            }

            ACTION_SEND_MULTIPLE -> buildList {
                extras.multiple.forEach(::add)
                extras.clipItems.forEach(::add)
            }

            else -> emptyList()
        }

        val seen = linkedSetOf<String>()
        val result = mutableListOf<SharedFile>()
        for (candidate in candidates) {
            val uri = candidate.uriString?.trim().orEmpty()
            if (uri.isEmpty()) {
                continue
            }
            if (!seen.add(uri)) {
                continue
            }
            val label = candidate.label?.trim()?.takeIf { it.isNotEmpty() }
            result += SharedFile(
                uriString = uri,
                displayName = label,
                mimeType = extras.mimeType?.trim()?.takeIf { it.isNotEmpty() },
            )
        }
        return result
    }

    private fun textUri(text: String?): String? {
        val value = text?.trim().orEmpty()
        return if (value.startsWith("content://") || value.startsWith("file://")) value else null
    }
}

/**
 * Thin Android adapter around [ShareIntentParser]. It only reads intent extras;
 * all copy and hashing happens later on an I/O dispatcher.
 */
object ShareIntent {

    @Suppress("DEPRECATION")
    fun read(intent: Intent?): List<SharedFile> {
        if (intent == null) {
            return emptyList()
        }
        val action = intent.action
        if (action != Intent.ACTION_SEND && action != Intent.ACTION_SEND_MULTIPLE) {
            return emptyList()
        }

        val single = intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)
        val multiple = intent.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM)
            .orEmpty()
            .map { ShareUri(it?.toString()) }
        val clips = buildList {
            val clip = intent.clipData ?: return@buildList
            val label = clip.description?.label?.toString()
            for (index in 0 until clip.itemCount) {
                add(ShareUri(clip.getItemAt(index).uri?.toString(), label))
            }
        }

        return ShareIntentParser.parse(
            ShareExtras(
                action = action,
                mimeType = intent.type,
                single = single?.let { ShareUri(it.toString()) },
                multiple = multiple,
                clipItems = clips,
                text = intent.getStringExtra(Intent.EXTRA_TEXT),
            ),
        )
    }
}
