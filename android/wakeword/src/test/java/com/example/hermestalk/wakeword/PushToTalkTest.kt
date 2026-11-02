package com.example.hermestalk.wakeword

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PushToTalkTest {

    @Test
    fun pressAndReleaseRequestsASession() {
        val controller = PushToTalkController()
        val pressed = controller.pressStarted()
        assertEquals(PushToTalkState.Pressed, pressed.state)
        assertTrue(pressed.effects.isEmpty())

        val released = controller.pressEnded()
        assertEquals(PushToTalkState.Released, released.state)
        assertEquals(
            listOf(PushToTalkEffect.StartSessionRequested),
            released.effects,
        )
    }

    @Test
    fun releaseWithoutPressDoesNotRequestASession() {
        val transition = PushToTalkMachine.reduce(
            PushToTalkState.Idle,
            PushToTalkEvent.PressEnded,
        )
        assertEquals(PushToTalkState.Idle, transition.state)
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun repeatedPressIsIdempotent() {
        val transition = PushToTalkMachine.reduce(
            PushToTalkState.Pressed,
            PushToTalkEvent.PressStarted,
        )
        assertEquals(PushToTalkState.Pressed, transition.state)
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun pressFromReleasedStartsANewPress() {
        val transition = PushToTalkMachine.reduce(
            PushToTalkState.Released,
            PushToTalkEvent.PressStarted,
        )
        assertEquals(PushToTalkState.Pressed, transition.state)
    }

    @Test
    fun captureFailureAbortsThePress() {
        val transition = PushToTalkMachine.reduce(
            PushToTalkState.Pressed,
            PushToTalkEvent.CaptureFailed("AudioRecord failed"),
        )
        assertEquals(PushToTalkState.Idle, transition.state)
        assertTrue(transition.effects.isEmpty())
    }

    @Test
    fun permissionDeniedReturnsToIdle() {
        val transition = PushToTalkMachine.reduce(
            PushToTalkState.Pressed,
            PushToTalkEvent.PermissionDenied,
        )
        assertEquals(PushToTalkState.Idle, transition.state)
    }

    @Test
    fun captureFailureOutsideAPressIsIgnored() {
        val transition = PushToTalkMachine.reduce(
            PushToTalkState.Idle,
            PushToTalkEvent.CaptureFailed("nothing was recording"),
        )
        assertEquals(PushToTalkState.Idle, transition.state)
    }

    @Test
    fun releasedStateCanBeResetToIdle() {
        val controller = PushToTalkController()
        controller.pressStarted()
        controller.pressEnded()
        assertEquals(PushToTalkState.Released, controller.state.value)
        controller.reset()
        assertEquals(PushToTalkState.Idle, controller.state.value)
    }
}
