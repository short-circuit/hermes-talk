package com.example.hermestalk.call

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import androidx.core.content.ContextCompat

/** Discovers, selects and monitors audio routes for a voice call. */
interface AudioRouteController {
    /** Routes currently available on this device. */
    fun availableRoutes(): List<AudioRoute>

    /** The route the controller last selected. */
    fun currentRoute(): AudioRoute

    /** Select [route]. Returns true when the platform accepted it. */
    fun select(route: AudioRoute): Boolean

    /** Start observing device and headset changes. */
    fun startMonitoring(onChanged: (List<AudioRoute>) -> Unit)

    /** Stop observing device changes. */
    fun stopMonitoring()

    /** Enter the voice-communication audio mode. */
    fun enterCommunicationMode()

    /** Leave the voice-communication audio mode. */
    fun exitCommunicationMode()

    /** Clear the selected communication device. */
    fun clearDevice()
}

/**
 * [AudioManager]-backed route controller. API 31+ uses communication devices;
 * older releases fall back to speakerphone and Bluetooth SCO toggles. Every
 * platform call is wrapped because audio routing failures must never crash a
 * call.
 */
class AndroidAudioRouteController(context: Context) : AudioRouteController {

    private val appContext = context.applicationContext
    private val audioManager =
        appContext.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private var selectedRoute = AudioRoute.Earpiece
    private var monitoring = false
    private var onRoutesChanged: ((List<AudioRoute>) -> Unit)? = null

    private val deviceCallback = object : AudioDeviceCallback() {
        override fun onAudioDevicesAdded(addedDevices: Array<out AudioDeviceInfo>) {
            notifyChanged()
        }

        override fun onAudioDevicesRemoved(removedDevices: Array<out AudioDeviceInfo>) {
            notifyChanged()
        }
    }

    private val headsetReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            notifyChanged()
        }
    }

    override fun availableRoutes(): List<AudioRoute> {
        return try {
            val routes = mutableListOf(AudioRoute.Earpiece, AudioRoute.Speaker)
            val outputs = audioManager.getDevices(AudioManager.GET_DEVICES_OUTPUTS)
            if (outputs.any { isWired(it) }) {
                routes += AudioRoute.WiredHeadset
            }
            if (outputs.any { isBluetooth(it) }) {
                routes += AudioRoute.Bluetooth
            }
            routes.distinct()
        } catch (_: Exception) {
            listOf(AudioRoute.Earpiece, AudioRoute.Speaker)
        }
    }

    override fun currentRoute(): AudioRoute = selectedRoute

    override fun select(route: AudioRoute): Boolean {
        if (route !in availableRoutes()) {
            return false
        }
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                selectCommunicationDevice(route)
            } else {
                selectLegacyRoute(route)
            }
        } catch (_: Exception) {
            false
        }
    }

    private fun selectCommunicationDevice(route: AudioRoute): Boolean {
        val device = audioManager.availableCommunicationDevices.firstOrNull {
            matchesRoute(it, route)
        } ?: return false
        val accepted = audioManager.setCommunicationDevice(device)
        if (accepted) {
            selectedRoute = route
        }
        return accepted
    }

    private fun selectLegacyRoute(route: AudioRoute): Boolean {
        when (route) {
            AudioRoute.Speaker -> {
                audioManager.stopBluetoothSco()
                audioManager.isBluetoothScoOn = false
                audioManager.isSpeakerphoneOn = true
            }

            AudioRoute.Bluetooth -> {
                audioManager.isSpeakerphoneOn = false
                audioManager.startBluetoothSco()
                audioManager.isBluetoothScoOn = true
            }

            AudioRoute.WiredHeadset,
            AudioRoute.Earpiece,
            -> {
                audioManager.stopBluetoothSco()
                audioManager.isBluetoothScoOn = false
                audioManager.isSpeakerphoneOn = false
            }
        }
        selectedRoute = route
        return true
    }

    override fun startMonitoring(onChanged: (List<AudioRoute>) -> Unit) {
        if (monitoring) {
            return
        }
        monitoring = true
        onRoutesChanged = onChanged
        try {
            audioManager.registerAudioDeviceCallback(
                deviceCallback,
                Handler(Looper.getMainLooper()),
            )
            val filter = IntentFilter().apply {
                addAction(Intent.ACTION_HEADSET_PLUG)
                addAction(AudioManager.ACTION_AUDIO_BECOMING_NOISY)
            }
            ContextCompat.registerReceiver(
                appContext,
                headsetReceiver,
                filter,
                ContextCompat.RECEIVER_NOT_EXPORTED,
            )
        } catch (_: Exception) {
            // Monitoring is best effort; route selection still works.
        }
    }

    override fun stopMonitoring() {
        if (!monitoring) {
            return
        }
        monitoring = false
        onRoutesChanged = null
        try {
            audioManager.unregisterAudioDeviceCallback(deviceCallback)
        } catch (_: Exception) {
            // Ignore an already-unregistered callback.
        }
        try {
            appContext.unregisterReceiver(headsetReceiver)
        } catch (_: Exception) {
            // Ignore an already-unregistered receiver.
        }
    }

    override fun enterCommunicationMode() {
        try {
            audioManager.mode = AudioManager.MODE_IN_COMMUNICATION
        } catch (_: Exception) {
            // Keep the previous mode when the platform refuses the change.
        }
    }

    override fun exitCommunicationMode() {
        clearDevice()
        try {
            audioManager.mode = AudioManager.MODE_NORMAL
        } catch (_: Exception) {
            // Keep the previous mode when the platform refuses the change.
        }
    }

    override fun clearDevice() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                audioManager.clearCommunicationDevice()
            } else {
                audioManager.isSpeakerphoneOn = false
                audioManager.stopBluetoothSco()
                audioManager.isBluetoothScoOn = false
            }
        } catch (_: Exception) {
            // Clearing a missing device is a no-op.
        }
        selectedRoute = AudioRoute.Earpiece
    }

    private fun notifyChanged() {
        onRoutesChanged?.invoke(availableRoutes())
    }

    private fun isWired(device: AudioDeviceInfo): Boolean =
        device.type == AudioDeviceInfo.TYPE_WIRED_HEADSET ||
            device.type == AudioDeviceInfo.TYPE_WIRED_HEADPHONES ||
            device.type == AudioDeviceInfo.TYPE_USB_HEADSET

    private fun isBluetooth(device: AudioDeviceInfo): Boolean =
        device.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP ||
            device.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO ||
            device.type == AudioDeviceInfo.TYPE_BLE_HEADSET ||
            device.type == AudioDeviceInfo.TYPE_BLE_SPEAKER

    private fun matchesRoute(device: AudioDeviceInfo, route: AudioRoute): Boolean = when (route) {
        AudioRoute.Earpiece -> device.type == AudioDeviceInfo.TYPE_BUILTIN_EARPIECE
        AudioRoute.Speaker ->
            device.type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER ||
                device.type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER_SAFE

        AudioRoute.WiredHeadset -> isWired(device)
        AudioRoute.Bluetooth -> isBluetooth(device)
    }
}
