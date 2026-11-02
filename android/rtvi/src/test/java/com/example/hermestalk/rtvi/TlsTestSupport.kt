package com.example.hermestalk.rtvi

import java.io.Closeable
import java.io.IOException
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.security.KeyFactory
import java.security.KeyStore
import java.security.PrivateKey
import java.security.cert.Certificate
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import java.security.spec.PKCS8EncodedKeySpec
import java.util.Base64
import javax.net.ssl.KeyManagerFactory
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLServerSocket
import javax.net.ssl.SSLSocket

/**
 * Test-only TLS fixtures. The PEM certificates and base64 PKCS#8 keys live in
 * `android/tls-fixtures` and are copied onto the test resource classpath by the
 * module build script.
 */
object TlsTestSupport {

    fun resourceBytes(name: String): ByteArray {
        val stream = TlsTestSupport::class.java.getResourceAsStream("/$name")
            ?: Thread.currentThread().contextClassLoader?.getResourceAsStream(name)
            ?: error("Missing test resource: $name")
        return stream.use { it.readBytes() }
    }

    fun certificate(name: String): X509Certificate {
        val factory = CertificateFactory.getInstance("X.509")
        return resourceBytes(name).inputStream().use {
            factory.generateCertificate(it) as X509Certificate
        }
    }

    fun privateKey(name: String): PrivateKey {
        val encoded = String(resourceBytes(name), Charsets.US_ASCII).trim()
        val der = Base64.getDecoder().decode(encoded)
        return KeyFactory.getInstance("RSA").generatePrivate(PKCS8EncodedKeySpec(der))
    }

    fun serverContext(certName: String, keyName: String): SSLContext {
        val certificate = certificate(certName)
        val privateKey = privateKey(keyName)
        val password = charArrayOf('t', 'e', 's', 't')
        val store = KeyStore.getInstance("PKCS12")
        store.load(null, null)
        store.setKeyEntry("relay", privateKey, password, arrayOf<Certificate>(certificate))
        val keyManagers = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm())
        keyManagers.init(store, password)
        return SSLContext.getInstance("TLS").apply {
            init(keyManagers.keyManagers, null, null)
        }
    }

    fun pinFor(certName: String): String = SpkiPin.fromCertificate(certificate(certName))

    fun certificateShaFor(certName: String): String =
        SpkiPin.certificateSha256(certificate(certName))

    /** A loopback server that is closed by the test in a `finally` block. */
    class TestServer internal constructor(
        private val socket: ServerSocket,
        private val thread: Thread,
    ) : Closeable {
        val port: Int = socket.localPort

        override fun close() {
            try {
                socket.close()
            } catch (_: IOException) {
                // Already closed.
            }
            thread.join(3000)
        }
    }

    fun startTlsServer(context: SSLContext, respondToHttp: Boolean): TestServer {
        val socket = context.serverSocketFactory.createServerSocket(
            0,
            50,
            InetAddress.getByName("127.0.0.1"),
        ) as SSLServerSocket
        socket.needClientAuth = false
        return start(socket, tls = true, respondToHttp = respondToHttp)
    }

    fun startPlainServer(respondToHttp: Boolean): TestServer {
        val socket = ServerSocket(0, 50, InetAddress.getByName("127.0.0.1"))
        return start(socket, tls = false, respondToHttp = respondToHttp)
    }

    /**
     * A loopback server that accepts a connection and closes it immediately, so
     * a TLS client sees a transport-level handshake failure ("connection
     * closed") rather than a certificate rejection.
     */
    fun startClosingServer(): TestServer {
        val socket = ServerSocket(0, 50, InetAddress.getByName("127.0.0.1"))
        val thread = Thread {
            while (!socket.isClosed) {
                val accepted = try {
                    socket.accept()
                } catch (_: IOException) {
                    break
                }
                accepted.close()
            }
        }
        thread.isDaemon = true
        thread.start()
        return TestServer(socket, thread)
    }

    private fun start(socket: ServerSocket, tls: Boolean, respondToHttp: Boolean): TestServer {
        val thread = Thread {
            while (!socket.isClosed) {
                val accepted = try {
                    socket.accept()
                } catch (_: IOException) {
                    break
                }
                try {
                    accepted.use { handle(it, tls, respondToHttp) }
                } catch (_: Exception) {
                    // A client abort during a failed handshake is expected.
                }
            }
        }
        thread.isDaemon = true
        thread.start()
        return TestServer(socket, thread)
    }

    private fun handle(socket: Socket, tls: Boolean, respondToHttp: Boolean) {
        if (tls) {
            (socket as SSLSocket).startHandshake()
        }
        if (respondToHttp) {
            respond(socket)
        }
    }

    private fun respond(socket: Socket) {
        val reader = socket.getInputStream().bufferedReader(Charsets.US_ASCII)
        val requestLine = reader.readLine() ?: return
        while (true) {
            val line = reader.readLine() ?: break
            if (line.isEmpty()) {
                break
            }
        }
        val isHealth = requestLine.contains("/health")
        val body = if (isHealth) "{\"status\":\"ok\"}" else "{\"error\":\"not found\"}"
        val status = if (isHealth) "200 OK" else "404 Not Found"
        val response = "HTTP/1.1 $status\r\n" +
            "Content-Type: application/json\r\n" +
            "Content-Length: ${body.toByteArray(Charsets.UTF_8).size}\r\n" +
            "Connection: close\r\n\r\n" +
            body
        socket.getOutputStream().write(response.toByteArray(Charsets.US_ASCII))
        socket.getOutputStream().flush()
    }
}
