package com.example.hermestalk.rtvi

import java.net.Socket
import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import java.util.Base64
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLEngine
import javax.net.ssl.SSLSocket
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.TrustManager
import javax.net.ssl.X509ExtendedTrustManager

/**
 * Certificate pin helpers for the internal relay certificate.
 *
 * The relay terminates TLS with an internal CA that Android does not trust by
 * default. The app therefore pins the leaf certificate's SubjectPublicKeyInfo
 * (SPKI) SHA-256 instead of trusting a CA. A matching pin is the entire trust
 * decision; hostname verification is still performed by the HTTP client.
 */
object SpkiPin {

    /**
     * Normalize a pin to the canonical `sha256/<standard base64 with padding>`
     * form. Accepted inputs are `sha256/<base64>`, bare base64, or 64 hex
     * characters with optional `:` or space separators. Anything else, or a
     * decoded digest that is not exactly 32 bytes, returns null.
     */
    fun normalize(raw: String): String? {
        val trimmed = raw.trim()
        if (trimmed.isEmpty()) {
            return null
        }
        val body = if (trimmed.startsWith("sha256/", ignoreCase = true)) {
            trimmed.substring("sha256/".length).trim()
        } else {
            trimmed
        }
        if (body.isEmpty()) {
            return null
        }

        val digest = decodeHex(body) ?: try {
            Base64.getDecoder().decode(body)
        } catch (_: IllegalArgumentException) {
            return null
        }
        if (digest.size != 32) {
            return null
        }
        return "sha256/" + Base64.getEncoder().encodeToString(digest)
    }

    /** Canonical SPKI pin: `sha256/` plus base64(SHA-256(publicKey.encoded)). */
    fun fromCertificate(cert: X509Certificate): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(cert.publicKey.encoded)
        return "sha256/" + Base64.getEncoder().encodeToString(digest)
    }

    /** Certificate fingerprint: `sha256/` plus base64(SHA-256(cert.encoded)). */
    fun certificateSha256(cert: X509Certificate): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(cert.encoded)
        return "sha256/" + Base64.getEncoder().encodeToString(digest)
    }

    /**
     * Constant-time comparison of [pin] against the SPKI of [cert]. Invalid or
     * non-canonical pins are normalized first; an unparseable pin never matches.
     */
    fun matches(pin: String, cert: X509Certificate): Boolean {
        val canonical = normalize(pin) ?: return false
        val expected = try {
            Base64.getDecoder().decode(canonical.substringAfter("sha256/"))
        } catch (_: IllegalArgumentException) {
            return false
        }
        val observed = MessageDigest.getInstance("SHA-256").digest(cert.publicKey.encoded)
        return MessageDigest.isEqual(expected, observed)
    }

    private fun decodeHex(raw: String): ByteArray? {
        val compact = raw.filterNot { it == ':' || it == ' ' }
        if (compact.length != 64) {
            return null
        }
        val out = ByteArray(32)
        for (index in out.indices) {
            val high = Character.digit(compact[index * 2], 16)
            val low = Character.digit(compact[index * 2 + 1], 16)
            if (high < 0 || low < 0) {
                return null
            }
            out[index] = ((high shl 4) or low).toByte()
        }
        return out
    }
}

/**
 * Raised when the relay leaf certificate does not match the configured pin.
 * The message includes both fingerprints so the failure can be explained in
 * the UI without logging certificate material elsewhere.
 */
class PinMismatchException(val expectedPin: String, val observedPin: String) :
    CertificateException("Certificate pin mismatch: expected $expectedPin, observed $observedPin")

/**
 * Fail-closed trust manager that accepts a server certificate only when its
 * SPKI SHA-256 matches [pin].
 *
 * A matching pin is the trust decision: this manager intentionally does NOT
 * also validate the chain against a CA store, because the relay's internal CA
 * is not installed in the Android system store. Hostname verification is still
 * performed by the HTTP client. There is no trust-all fallback.
 */
class PinnedTrustManager(private val pin: String) : X509ExtendedTrustManager() {

    override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        checkServerTrusted(chain)
    }

    override fun checkServerTrusted(
        chain: Array<out X509Certificate>?,
        authType: String?,
        socket: Socket?,
    ) {
        checkServerTrusted(chain)
    }

    override fun checkServerTrusted(
        chain: Array<out X509Certificate>?,
        authType: String?,
        engine: SSLEngine?,
    ) {
        checkServerTrusted(chain)
    }

    private fun checkServerTrusted(chain: Array<out X509Certificate>?) {
        val leaf = chain?.firstOrNull()
            ?: throw CertificateException("empty certificate chain")
        if (!SpkiPin.matches(pin, leaf)) {
            throw PinMismatchException(pin, SpkiPin.fromCertificate(leaf))
        }
    }

    override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        // Client certificates are not used by the relay.
    }

    override fun checkClientTrusted(
        chain: Array<out X509Certificate>?,
        authType: String?,
        socket: Socket?,
    ) {
        // Client certificates are not used by the relay.
    }

    override fun checkClientTrusted(
        chain: Array<out X509Certificate>?,
        authType: String?,
        engine: SSLEngine?,
    ) {
        // Client certificates are not used by the relay.
    }

    override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
}

/**
 * Trust manager used ONLY to read the peer certificate during fingerprint
 * discovery. It records the leaf certificate and accepts it; it never guards a
 * session. Sessions are guarded by [PinnedTrustManager] or the platform trust
 * store.
 */
class CapturingTrustManager : X509ExtendedTrustManager() {

    @Volatile
    private var captured: X509Certificate? = null

    /** The most recently observed leaf certificate, if any. */
    val capturedLeaf: X509Certificate?
        get() = captured

    override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        captured = chain?.firstOrNull()
    }

    override fun checkServerTrusted(
        chain: Array<out X509Certificate>?,
        authType: String?,
        socket: Socket?,
    ) {
        captured = chain?.firstOrNull()
    }

    override fun checkServerTrusted(
        chain: Array<out X509Certificate>?,
        authType: String?,
        engine: SSLEngine?,
    ) {
        captured = chain?.firstOrNull()
    }

    override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        // Client certificates are not used by the relay.
    }

    override fun checkClientTrusted(
        chain: Array<out X509Certificate>?,
        authType: String?,
        socket: Socket?,
    ) {
        // Client certificates are not used by the relay.
    }

    override fun checkClientTrusted(
        chain: Array<out X509Certificate>?,
        authType: String?,
        engine: SSLEngine?,
    ) {
        // Client certificates are not used by the relay.
    }

    override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
}

/** TLS trust choices. Every variant is fail-closed; none trusts all peers. */
sealed interface TlsPolicy {
    /** Accept only a leaf certificate whose SPKI matches [pin]. */
    data class Pinned(val pin: String) : TlsPolicy

    /** Use the platform trust store (system and user-installed CAs). */
    data object PlatformCa : TlsPolicy

    /** Plain HTTP on loopback, used only for the adb-reverse dev path. */
    data object CleartextLoopback : TlsPolicy
}

/** Factory helpers and fingerprint discovery for the relay certificate. */
object TlsTrust {

    /** Certificate details returned by [discover]. */
    data class Discovery(
        val spkiPin: String,
        val certificateSha256: String,
        val subject: String,
        val issuer: String,
        val notAfterEpochMs: Long,
        val expired: Boolean,
    )

    /** TLS context that accepts only a leaf matching [pin]. */
    fun sslContextForPin(pin: String): SSLContext =
        SSLContext.getInstance("TLS").apply {
            init(null, arrayOf<TrustManager>(PinnedTrustManager(pin)), null)
        }

    /** TLS context backed by the platform trust store. */
    fun sslContextPlatformTrust(): SSLContext =
        SSLContext.getInstance("TLS").apply {
            init(null, null, null)
        }

    /** Socket factory that accepts only a leaf matching [pin]. */
    fun socketFactoryForPin(pin: String): SSLSocketFactory =
        sslContextForPin(pin).socketFactory

    /** Socket factory backed by the platform trust store. */
    fun socketFactoryPlatformTrust(): SSLSocketFactory =
        sslContextPlatformTrust().socketFactory

    /**
     * Connect to [host]:[port], perform a TLS handshake with a
     * [CapturingTrustManager], and return the peer certificate details.
     *
     * This deliberately does not verify the certificate: discovery is how the
     * user obtains the pin they will subsequently confirm. The socket is always
     * closed in `finally`.
     */
    @Throws(java.io.IOException::class, CertificateException::class)
    fun discover(host: String, port: Int, timeoutMs: Int = 5000): Discovery {
        val capturing = CapturingTrustManager()
        val context = SSLContext.getInstance("TLS").apply {
            init(null, arrayOf<TrustManager>(capturing), null)
        }
        val socket = context.socketFactory.createSocket(host, port) as SSLSocket
        try {
            socket.soTimeout = timeoutMs
            socket.startHandshake()
            val peer = socket.session.peerCertificates.firstOrNull() as? X509Certificate
                ?: capturing.capturedLeaf
                ?: throw CertificateException("No peer certificate presented")
            val notAfter = peer.notAfter.time
            return Discovery(
                spkiPin = SpkiPin.fromCertificate(peer),
                certificateSha256 = SpkiPin.certificateSha256(peer),
                subject = peer.subjectX500Principal.name,
                issuer = peer.issuerX500Principal.name,
                notAfterEpochMs = notAfter,
                expired = notAfter < System.currentTimeMillis(),
            )
        } finally {
            socket.close()
        }
    }
}
