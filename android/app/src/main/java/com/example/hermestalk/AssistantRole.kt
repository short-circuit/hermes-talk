package com.example.hermestalk

import android.app.role.RoleManager
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.annotation.RequiresApi

/** Whether this app currently holds the system assistant role. */
enum class AssistantRoleState {
    /** The platform or this device does not expose the assistant role. */
    Unavailable,

    /** The role is available but another app (or none) holds it. */
    NotHeld,

    /** This app holds the system assistant role. */
    Held,
}

/**
 * Reads and requests `android.app.role.ASSISTANT`, the role behind the system
 * assist gesture (a long press of the power button on most devices).
 *
 * The role is a user-controlled privilege, not an app permission. The app must
 * re-query [state] on every resume because the user can grant or revoke the role
 * at any time from system Settings. A revoked role only changes the Settings UI
 * (the card flips back to [AssistantRoleState.NotHeld]); it must never crash the
 * app or restart the foreground call service. The exported `ACTION_ASSIST`
 * intent filter keeps the assist gesture working even when the role is held by
 * another app.
 */
object AssistantRole {

    /**
     * Report the current assistant-role state without throwing. Any missing
     * platform service, unavailable role or exception is reported as
     * [AssistantRoleState.Unavailable].
     */
    fun state(context: Context): AssistantRoleState = runCatching {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            return@runCatching AssistantRoleState.Unavailable
        }
        val roleManager = context.getSystemService(RoleManager::class.java)
            ?: return@runCatching AssistantRoleState.Unavailable
        queryState(roleManager)
    }.getOrDefault(AssistantRoleState.Unavailable)

    /**
     * Build the system dialog that asks the user to grant the assistant role.
     * Returns null when the role is unavailable or the platform cannot create
     * the request. Never throws.
     */
    fun requestIntent(context: Context): Intent? = runCatching {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            return@runCatching null
        }
        val roleManager = context.getSystemService(RoleManager::class.java)
            ?: return@runCatching null
        if (!roleManager.isRoleAvailable(RoleManager.ROLE_ASSISTANT)) {
            return@runCatching null
        }
        roleManager.createRequestRoleIntent(RoleManager.ROLE_ASSISTANT)
    }.getOrNull()

    @RequiresApi(Build.VERSION_CODES.Q)
    private fun queryState(roleManager: RoleManager): AssistantRoleState {
        if (!roleManager.isRoleAvailable(RoleManager.ROLE_ASSISTANT)) {
            return AssistantRoleState.Unavailable
        }
        return if (roleManager.isRoleHeld(RoleManager.ROLE_ASSISTANT)) {
            AssistantRoleState.Held
        } else {
            AssistantRoleState.NotHeld
        }
    }
}
