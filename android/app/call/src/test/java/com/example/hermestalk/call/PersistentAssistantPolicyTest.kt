package com.example.hermestalk.call

import android.app.ForegroundServiceStartNotAllowedException
import android.app.Service
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * JVM policy coverage for the always-on assistant. These tests do not need a
 * device: the foreground-type order, wake-lock policy, notification selection,
 * restart mode and refusal classification are pure.
 */
class PersistentAssistantPolicyTest {

    @Test
    fun attemptOrderWithoutRecordAudioUsesSpecialUseFromApi29() {
        assertEquals(
            listOf(AssistantForegroundType.None),
            PersistentAssistant.attemptOrder(hasRecordAudio = false, sdkInt = 26),
        )
        for (sdk in listOf(29, 33, 34, 35)) {
            assertEquals(
                "sdk $sdk without RECORD_AUDIO",
                listOf(AssistantForegroundType.SpecialUse),
                PersistentAssistant.attemptOrder(hasRecordAudio = false, sdkInt = sdk),
            )
        }
    }

    @Test
    fun attemptOrderWithRecordAudioTriesMicrophoneThenSpecialUse() {
        assertEquals(
            listOf(AssistantForegroundType.None),
            PersistentAssistant.attemptOrder(hasRecordAudio = true, sdkInt = 26),
        )
        for (sdk in listOf(29, 33, 34, 35)) {
            assertEquals(
                "sdk $sdk with RECORD_AUDIO",
                listOf(
                    AssistantForegroundType.Microphone,
                    AssistantForegroundType.SpecialUse,
                ),
                PersistentAssistant.attemptOrder(hasRecordAudio = true, sdkInt = sdk),
            )
        }
    }

    @Test
    fun wakeLockIsHeldOnlyWhileListening() {
        assertTrue(PersistentAssistant.shouldHoldWakeLock(listening = true))
        assertFalse(PersistentAssistant.shouldHoldWakeLock(listening = false))
    }

    @Test
    fun notificationTextFollowsListeningState() {
        assertEquals(
            R.string.persistent_notification_text_listening,
            PersistentAssistant.notificationText(listening = true),
        )
        assertEquals(
            R.string.persistent_notification_text_idle,
            PersistentAssistant.notificationText(listening = false),
        )
    }

    @Test
    fun restartModeIsSticky() {
        assertEquals(Service.START_STICKY, PersistentAssistant.restartMode())
    }

    @Test
    fun isMicRefusalRecognisesTheThreeRefusalTypes() {
        assertTrue(
            PersistentAssistant.isMicRefusal(
                ForegroundServiceStartNotAllowedException("started from BOOT_COMPLETED"),
            ),
        )
        assertTrue(PersistentAssistant.isMicRefusal(SecurityException("RECORD_AUDIO revoked")))
        assertTrue(PersistentAssistant.isMicRefusal(IllegalStateException("not foreground")))
        assertFalse(PersistentAssistant.isMicRefusal(RuntimeException("unrelated")))
    }

    @Test
    fun micRefusalRecordIncludesTheExceptionClassAndMessage() {
        assertEquals(
            "java.lang.SecurityException: no while-in-use permission",
            PersistentAssistant.micRefusalRecord(
                SecurityException("no while-in-use permission"),
            ),
        )
    }

    @Test
    fun fallbackUsesSpecialUseAfterAMicrophoneRefusal() {
        val attempted = mutableListOf<AssistantForegroundType>()
        val failed = mutableListOf<AssistantForegroundType>()
        val accepted = PersistentAssistant.firstSuccessful(
            candidates = listOf(
                AssistantForegroundType.Microphone,
                AssistantForegroundType.SpecialUse,
            ),
            attempt = { type ->
                attempted += type
                if (type == AssistantForegroundType.Microphone) {
                    throw SecurityException("no while-in-use permission")
                }
            },
            onFailure = { type, _ -> failed += type },
        )

        assertEquals(AssistantForegroundType.SpecialUse, accepted)
        assertEquals(
            listOf(
                AssistantForegroundType.Microphone,
                AssistantForegroundType.SpecialUse,
            ),
            attempted,
        )
        assertEquals(listOf(AssistantForegroundType.Microphone), failed)
    }

    @Test
    fun fallbackReturnsNullWhenEveryForegroundTypeFails() {
        val attempted = mutableListOf<AssistantForegroundType>()
        val accepted = PersistentAssistant.firstSuccessful(
            candidates = listOf(
                AssistantForegroundType.Microphone,
                AssistantForegroundType.SpecialUse,
            ),
            attempt = { type ->
                attempted += type
                throw IllegalStateException(type.name)
            },
            onFailure = { _, _ -> },
        )

        assertNull(accepted)
        assertEquals(
            listOf(
                AssistantForegroundType.Microphone,
                AssistantForegroundType.SpecialUse,
            ),
            attempted,
        )
    }

    @Test
    fun settingsStoreDefaultsOffAndRoundTripsWithAFake() {
        val store = FakePersistentAssistantSettingsStore()
        assertFalse("the assistant must be opt-in", store.isEnabled())

        store.setEnabled(true)
        assertTrue(store.isEnabled())

        store.setEnabled(false)
        assertFalse(store.isEnabled())
    }

    @Test
    fun settingsStoreUsesThePlainPreferencesFile() {
        assertEquals(
            "hermes_talk_persistent",
            SharedPreferencesPersistentAssistantSettingsStore.PREFERENCES_FILE,
        )
        assertEquals(
            "assistant_enabled",
            SharedPreferencesPersistentAssistantSettingsStore.KEY_ENABLED,
        )
    }

    private class FakePersistentAssistantSettingsStore(
        initial: Boolean = false,
    ) : PersistentAssistantSettingsStore {
        private var enabled = initial

        override fun isEnabled(): Boolean = enabled

        override fun setEnabled(enabled: Boolean) {
            this.enabled = enabled
        }
    }
}
