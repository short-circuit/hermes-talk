package com.example.hermestalk.spike

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.util.Log
import android.view.ViewGroup
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

/**
 * Throwaway UI: start/stop the spike call, choose the relay endpoint, request the battery-optimisation
 * exemption. Everything the app learns is written to logcat under [TAG].
 */
class MainActivity : Activity() {

    private lateinit var status: TextView
    private lateinit var hostField: EditText
    private lateinit var portField: EditText
    private lateinit var toneBox: CheckBox
    private lateinit var mediaUsageBox: CheckBox
    private lateinit var startButton: Button
    private lateinit var stopButton: Button

    private val handler = Handler(Looper.getMainLooper())
    private val ticker = object : Runnable {
        override fun run() {
            refreshStatus()
            handler.postDelayed(this, 1000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(buildLayout())
        logSdkProbe()
        if (savedInstanceState == null) handleExtras(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleExtras(intent)
    }

    override fun onResume() {
        super.onResume()
        handler.post(ticker)
    }

    override fun onPause() {
        handler.removeCallbacks(ticker)
        super.onPause()
    }

    private fun buildLayout(): ScrollView {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }
        val title = TextView(this).apply {
            text = "Hermes Talk spike"
            textSize = 22f
        }
        status = TextView(this).apply {
            text = "idle"
            textSize = 14f
            setPadding(0, 24, 0, 24)
        }
        hostField = EditText(this).apply {
            hint = "relay host"
            setText(DEFAULT_HOST)
        }
        portField = EditText(this).apply {
            hint = "relay port"
            setText(DEFAULT_PORT.toString())
        }
        toneBox = CheckBox(this).apply {
            text = "Send generated 660 Hz tone upstream (no microphone)"
            isChecked = !packageManager.hasSystemFeature(PackageManager.FEATURE_MICROPHONE)
        }
        mediaUsageBox = CheckBox(this).apply {
            text = "Render with USAGE_MEDIA instead of VOICE_COMMUNICATION"
            isChecked = false
        }
        startButton = Button(this).apply {
            text = "Start call"
            setOnClickListener { startCallWithPermission() }
        }
        stopButton = Button(this).apply {
            text = "Stop call"
            setOnClickListener { stopCall() }
        }
        val exemptButton = Button(this).apply {
            text = "Allow unrestricted battery use"
            setOnClickListener { requestBatteryExemption() }
        }
        listOf(title, status, hostField, portField, toneBox, mediaUsageBox, startButton, stopButton, exemptButton)
            .forEach { root.addView(it, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT) }
        return ScrollView(this).apply { addView(root) }
    }

    /**
     * Reflection-only probe: the class names are resolved if present, ignored if not, so the same
     * source compiles in both the dependency-free and the Pipecat-linked build.
     */
    private fun logSdkProbe() {
        for (name in listOf(
            "ai.pipecat.client.small_webrtc_transport.SmallWebRTCTransport",
            "org.webrtc.PeerConnectionFactory",
        )) {
            try {
                val cls = Class.forName(name)
                Log.i(TAG, "sdk-probe: loaded $name from ${cls.protectionDomain?.codeSource?.location}")
            } catch (t: Throwable) {
                Log.i(TAG, "sdk-probe: absent $name (${t.javaClass.simpleName})")
            }
        }
        try {
            System.loadLibrary("jingle_peerconnection_so")
            Log.i(TAG, "sdk-probe: loaded native jingle_peerconnection_so")
        } catch (t: Throwable) {
            Log.i(TAG, "sdk-probe: native library absent (${t.javaClass.simpleName})")
        }
    }

    private fun handleExtras(intent: Intent?) {
        if (intent == null) return
        intent.getStringExtra(CallService.EXTRA_HOST)?.let { hostField.setText(it) }
        if (intent.hasExtra(CallService.EXTRA_PORT)) {
            portField.setText(intent.getIntExtra(CallService.EXTRA_PORT, DEFAULT_PORT).toString())
        }
        if (intent.hasExtra(CallService.EXTRA_TONE_HZ)) {
            toneBox.isChecked = intent.getDoubleExtra(CallService.EXTRA_TONE_HZ, 0.0) > 0.0
        }
        if (intent.hasExtra(CallService.EXTRA_MEDIA_USAGE)) {
            mediaUsageBox.isChecked = intent.getBooleanExtra(CallService.EXTRA_MEDIA_USAGE, false)
        }
        if (intent.getBooleanExtra(CallService.EXTRA_AUTOSTART, false)) startCallWithPermission()
    }

    private fun startCallWithPermission() {
        val wanted = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            wanted += Manifest.permission.POST_NOTIFICATIONS
        }
        val missing = wanted.filter {
            checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isNotEmpty()) {
            requestPermissions(missing.toTypedArray(), REQUEST_PERMISSIONS)
            return
        }
        startCall()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQUEST_PERMISSIONS) return
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            startCall()
        } else {
            status.text = "RECORD_AUDIO denied — a microphone foreground service cannot start"
            Log.w(TAG, "ui: RECORD_AUDIO denied")
        }
    }

    private fun startCall() {
        val host = hostField.text.toString().ifBlank { DEFAULT_HOST }
        val port = portField.text.toString().toIntOrNull() ?: DEFAULT_PORT
        val toneHz = if (toneBox.isChecked) 660.0 else 0.0
        val intent = Intent(this, CallService::class.java)
            .setAction(CallService.ACTION_START)
            .putExtra(CallService.EXTRA_HOST, host)
            .putExtra(CallService.EXTRA_PORT, port)
            .putExtra(CallService.EXTRA_TONE_HZ, toneHz)
            .putExtra(CallService.EXTRA_MEDIA_USAGE, mediaUsageBox.isChecked)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }
        Log.i(TAG, "ui: start requested host=$host port=$port tone_hz=$toneHz media_usage=${mediaUsageBox.isChecked}")
    }

    private fun stopCall() {
        startService(Intent(this, CallService::class.java).setAction(CallService.ACTION_STOP))
        Log.i(TAG, "ui: stop requested")
    }

    private fun requestBatteryExemption() {
        val pm = getSystemService(PowerManager::class.java)
        if (pm?.isIgnoringBatteryOptimizations(packageName) == true) {
            status.text = "battery: already unrestricted"
            return
        }
        val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
            .setData(Uri.parse("package:$packageName"))
        try {
            startActivity(intent)
        } catch (t: Throwable) {
            Log.w(TAG, "ui: battery exemption dialog unavailable: ${t.message}")
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
    }

    private fun refreshStatus() {
        val pm = getSystemService(PowerManager::class.java)
        val exempt = pm?.isIgnoringBatteryOptimizations(packageName) == true
        val idle = pm?.isDeviceIdleMode == true
        val mic = packageManager.hasSystemFeature(PackageManager.FEATURE_MICROPHONE)
        status.text = buildString {
            appendLine("service running: ${CallService.isRunning}")
            appendLine("relay: ${hostField.text}:${portField.text}")
            appendLine("battery exemption: $exempt")
            appendLine("device idle (doze): $idle")
            append("microphone feature: $mic")
        }
    }

    private companion object {
        const val REQUEST_PERMISSIONS = 1
    }
}
