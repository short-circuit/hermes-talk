package com.example.hermestalk.call

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.PowerManager
import android.provider.Settings

/** Battery-optimisation state and the intent that asks for an exemption. */
interface BatteryOptimisation {
    /** True when the app is already exempt from battery optimisation. */
    fun isIgnoring(): Boolean

    /** Intent that opens the battery-optimisation exemption UI. */
    fun requestIntent(): Intent
}

/** [PowerManager]-backed battery-optimisation helper. */
class AndroidBatteryOptimisation(context: Context) : BatteryOptimisation {

    private val appContext = context.applicationContext

    override fun isIgnoring(): Boolean {
        return try {
            val powerManager =
                appContext.getSystemService(Context.POWER_SERVICE) as PowerManager
            powerManager.isIgnoringBatteryOptimizations(appContext.packageName)
        } catch (_: Exception) {
            false
        }
    }

    override fun requestIntent(): Intent {
        val request = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
            .setData(Uri.parse("package:${appContext.packageName}"))
        return try {
            if (appContext.packageManager.resolveActivity(request, 0) != null) {
                request
            } else {
                Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)
            }
        } catch (_: Exception) {
            Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)
        }
    }
}
