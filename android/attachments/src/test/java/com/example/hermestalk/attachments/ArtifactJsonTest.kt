package com.example.hermestalk.attachments

import com.example.hermestalk.rtvi.RelayEndpoint
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ArtifactJsonTest {

    @Test
    fun parsesAValidArtifactList() {
        val body = """
            {"artifacts":[{"id":"reports/2026/q1.pdf","name":"q1.pdf","mime":"application/pdf",
            "size_bytes":1234,"sha256":"abc123","created_at":"2026-01-01T00:00:00Z","expires_at":null}],
            "count":1,"limit":50,"retention_days":7}
        """.trimIndent()
        val parsed = ArtifactJson.parseList(body)
        assertTrue(parsed is ArtifactParseResult.Success)
        val list = (parsed as ArtifactParseResult.Success).list
        assertEquals(1, list.count)
        assertEquals(7, list.retentionDays)
        assertEquals("reports/2026/q1.pdf", list.artifacts.first().id)
        assertEquals(1234L, list.artifacts.first().sizeBytes)
    }

    @Test
    fun rejectsMalformedJson() {
        assertTrue(ArtifactJson.parseList("{not json") is ArtifactParseResult.Invalid)
    }

    @Test
    fun rejectsAnItemWithMissingRequiredFields() {
        val body = """{"artifacts":[{"id":"a","name":"a.pdf","mime":"application/pdf","size_bytes":1}]}"""
        assertTrue(ArtifactJson.parseList(body) is ArtifactParseResult.Invalid)
    }

    @Test
    fun parsesSpokenNotFoundError() {
        val parsed = ArtifactJson.parseError(
            """{"error":"artifact_not_found","detail":"missing","spoken":"That artifact could not be found."}""",
        )
        assertNotNull(parsed)
        assertEquals("artifact_not_found", parsed?.error)
        assertEquals("That artifact could not be found.", parsed?.spoken)
    }

    @Test
    fun encodesArtifactPathSegments() {
        val endpoint = (RelayEndpoint.parse("http://127.0.0.1:8790") as RelayEndpoint.ParseResult.Valid)
            .endpoint
        assertEquals(
            "reports/2026/q1%20final.pdf",
            ArtifactUrl.encodeId("reports/2026/q1 final.pdf"),
        )
        assertEquals(
            "http://127.0.0.1:8790/v1/artifacts/reports/2026/q1%20final.pdf",
            ArtifactUrl.downloadUrl(endpoint, "reports/2026/q1 final.pdf"),
        )
        assertEquals("a%2Bb/%C3%A9.txt", ArtifactUrl.encodeId("a+b/é.txt"))
    }
}
