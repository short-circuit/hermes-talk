package com.example.hermestalk.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val LightColors = lightColorScheme(
    primary = Color(0xFF2E6B4F),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFB8F1D4),
    onPrimaryContainer = Color(0xFF002114),
    secondary = Color(0xFF4F6356),
    onSecondary = Color.White,
    background = Color(0xFFF6FBF7),
    surface = Color(0xFFF6FBF7),
    error = Color(0xFFB3261E),
    onError = Color.White,
)

private val DarkColors = darkColorScheme(
    primary = Color(0xFF9DD6B8),
    onPrimary = Color(0xFF003825),
    primaryContainer = Color(0xFF175239),
    onPrimaryContainer = Color(0xFFB8F1D4),
    secondary = Color(0xFFB6CCBC),
    onSecondary = Color(0xFF213529),
    background = Color(0xFF0F1511),
    surface = Color(0xFF0F1511),
    error = Color(0xFFF2B8B5),
    onError = Color(0xFF601410),
)

/** Deterministic Material 3 theme for Hermes Talk. */
@Composable
fun HermesTalkTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = if (isSystemInDarkTheme()) DarkColors else LightColors,
        content = content,
    )
}
