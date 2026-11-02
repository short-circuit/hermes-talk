package com.example.hermestalk.wakeword

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.io.FileOutputStream
import java.nio.charset.StandardCharsets

class FalseTriggerLogTest {

    private lateinit var directory: File

    @Before
    fun setUp() {
        directory = File(
            System.getProperty("java.io.tmpdir"),
            "wakeword-log-${System.nanoTime()}",
        )
        directory.mkdirs()
    }

    @After
    fun tearDown() {
        directory.deleteRecursively()
    }

    private fun entry(
        timestamp: Long,
        source: WakeWordSource = WakeWordSource.WakeWord,
        keyword: String = "HEY HERMES",
    ) = FalseTriggerEntry(
        timestampMillis = timestamp,
        keyword = keyword,
        source = source,
        threshold = 0.15f,
        listeningMillis = 42L,
    )

    @Test
    fun appendAndReadRecentRoundTrip() {
        val log = FalseTriggerLog(directory, 65_536L)
        assertTrue(log.append(entry(1_000L)))
        assertTrue(log.append(entry(2_000L, WakeWordSource.PushToTalk)))

        val recent = log.readRecent(10)
        assertEquals(2, recent.size)
        assertEquals(1_000L, recent[0].timestampMillis)
        assertEquals(WakeWordSource.WakeWord, recent[0].source)
        assertEquals(WakeWordSource.PushToTalk, recent[1].source)
        assertEquals(0.15f, recent[0].threshold, 0.0001f)
        assertEquals(42L, recent[0].listeningMillis)
    }

    @Test
    fun readRecentReturnsOnlyTheRequestedTail() {
        val log = FalseTriggerLog(directory, 65_536L)
        for (index in 1..5) {
            log.append(entry(index.toLong() * 1_000L))
        }
        val recent = log.readRecent(2)
        assertEquals(2, recent.size)
        assertEquals(4_000L, recent[0].timestampMillis)
        assertEquals(5_000L, recent[1].timestampMillis)
    }

    @Test
    fun capRotatesToASingleBackup() {
        val log = FalseTriggerLog(directory, 220L)
        for (index in 1..20) {
            assertTrue(log.append(entry(index.toLong())))
        }
        val current = File(directory, "events.log")
        val backup = File(directory, "events.log.1")
        assertTrue(current.isFile)
        assertTrue(backup.isFile)
        assertTrue(current.length() <= 220L)
        assertTrue(backup.length() <= 220L)
        assertTrue(log.readRecent(50).isNotEmpty())
    }

    @Test
    fun recoverySkipsAPartiallyWrittenLastLine() {
        val log = FalseTriggerLog(directory, 65_536L)
        log.append(entry(1_000L))
        log.append(entry(2_000L))
        FileOutputStream(File(directory, "events.log"), true).use { output ->
            output.write("{\"ts\":3000,\"keyword\":\"HEY".toByteArray(StandardCharsets.UTF_8))
        }
        val recent = log.readRecent(10)
        assertEquals(2, recent.size)
        assertEquals(2_000L, recent[1].timestampMillis)
    }

    @Test
    fun entriesDoNotContainAbsolutePathsOrPrivateData() {
        val log = FalseTriggerLog(directory, 65_536L)
        log.append(entry(1_000L, keyword = "HEY HERMES"))
        val text = File(directory, "events.log").readText(StandardCharsets.UTF_8)
        assertFalse(text.contains(directory.absolutePath))
        assertFalse(text.contains("/home/"))
        assertTrue(text.contains("\"keyword\":\"HEY HERMES\""))
    }

    @Test
    fun escapedKeywordSurvivesRoundTrip() {
        val log = FalseTriggerLog(directory, 65_536L)
        log.append(entry(1_000L, keyword = "HEY \"HERMES\""))
        val recent = log.readRecent(1)
        assertEquals(1, recent.size)
        assertEquals("HEY \"HERMES\"", recent[0].keyword)
    }

    @Test
    fun zeroCountReturnsEmpty() {
        val log = FalseTriggerLog(directory, 65_536L)
        log.append(entry(1_000L))
        assertTrue(log.readRecent(0).isEmpty())
        assertTrue(log.readRecent(-1).isEmpty())
    }
}
