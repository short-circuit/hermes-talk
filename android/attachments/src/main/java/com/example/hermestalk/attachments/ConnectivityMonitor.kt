package com.example.hermestalk.attachments

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network

/** Injectable connectivity seam so queue tests run without Android. */
interface ConnectivityMonitor {
    fun start(onAvailable: () -> Unit)

    fun stop()
}

/** No-op monitor used by JVM tests and previews. */
class NoopConnectivityMonitor : ConnectivityMonitor {
    override fun start(onAvailable: () -> Unit) = Unit

    override fun stop() = Unit
}

/**
 * Production monitor. The app already holds ACCESS_NETWORK_STATE. A network
 * becoming available wakes the upload worker immediately so a queued payload
 * does not wait for the next backoff window.
 */
class AndroidConnectivityMonitor(context: Context) : ConnectivityMonitor {

    private val manager = context.applicationContext
        .getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
    private var callback: ConnectivityManager.NetworkCallback? = null

    override fun start(onAvailable: () -> Unit) {
        val connectivity = manager ?: return
        if (callback != null) {
            return
        }
        val networkCallback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                onAvailable()
            }
        }
        callback = networkCallback
        runCatching { connectivity.registerDefaultNetworkCallback(networkCallback) }
            .onFailure { callback = null }
    }

    override fun stop() {
        val connectivity = manager ?: return
        val networkCallback = callback ?: return
        callback = null
        runCatching { connectivity.unregisterNetworkCallback(networkCallback) }
    }
}
