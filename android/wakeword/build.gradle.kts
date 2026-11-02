import java.net.URI
import java.security.MessageDigest
import org.gradle.api.GradleException
import org.gradle.api.file.RelativePath

plugins {
    id("com.android.library")
    id("org.jetbrains.kotlin.android")
}

val sherpaVersion = "v2.0.1"
val sherpaArchiveName = "sherpa-onnx-v2.0.1-android.tar.bz2"
val sherpaDownloadUrl =
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/$sherpaVersion/$sherpaArchiveName"
val sherpaSha256 = "39003753ec293425ed56723ea9b972b98841a490af3311670c3060548d654330"
val sherpaJniDir = layout.buildDirectory.dir("jniLibs/sherpa-onnx")

val fetchSherpaOnnxJniLibs = tasks.register("fetchSherpaOnnxJniLibs") {
    group = "build"
    description = "Downloads and extracts the sherpa-onnx Android JNI libraries at execution time."

    doLast {
        val destination = sherpaJniDir.get().asFile
        val marker = destination.resolve(".complete")
        val abis = listOf("arm64-v8a", "x86_64")

        fun extracted(): Boolean =
            marker.isFile &&
                marker.readText().trim() == sherpaSha256 &&
                abis.all { abi ->
                    val dir = destination.resolve(abi)
                    dir.isDirectory && dir.listFiles()?.any { it.extension == "so" } == true
                }

        if (extracted()) {
            logger.lifecycle("sherpa-onnx $sherpaVersion JNI libraries already extracted")
            return@doLast
        }

        val archive = layout.buildDirectory.file(sherpaArchiveName).get().asFile
        archive.parentFile.mkdirs()

        logger.lifecycle("downloading sherpa-onnx $sherpaVersion Android libraries")
        try {
            val connection = URI(sherpaDownloadUrl).toURL().openConnection()
            connection.connectTimeout = 15_000
            connection.readTimeout = 120_000
            connection.getInputStream().use { input ->
                archive.outputStream().use { output ->
                    input.copyTo(output)
                }
            }
        } catch (error: Exception) {
            throw GradleException(
                "sherpa-onnx $sherpaVersion: could not download $sherpaDownloadUrl: " +
                    (error.message ?: error.javaClass.simpleName),
                error,
            )
        }

        val digest = MessageDigest.getInstance("SHA-256")
        archive.inputStream().use { input ->
            val buffer = ByteArray(8192)
            var read: Int
            while (input.read(buffer).also { read = it } != -1) {
                digest.update(buffer, 0, read)
            }
        }
        val actual = digest.digest().joinToString("") { byte ->
            "%02x".format(byte.toInt() and 0xff)
        }
        if (actual != sherpaSha256) {
            archive.delete()
            throw GradleException(
                "sherpa-onnx $sherpaVersion: SHA-256 mismatch for $sherpaArchiveName. " +
                    "Expected $sherpaSha256 but got $actual. The download was discarded; " +
                    "check the network or the release checksum.",
            )
        }

        destination.deleteRecursively()
        destination.mkdirs()
        try {
            val archiveTree = project.tarTree(project.resources.bzip2(archive))
            project.copy {
                from(archiveTree) {
                    include("jniLibs/arm64-v8a/**")
                    include("jniLibs/x86_64/**")
                    eachFile {
                        val segments = relativePath.segments
                        if (segments.isNotEmpty() && segments[0] == "jniLibs") {
                            relativePath = RelativePath(true, *segments.drop(1).toTypedArray())
                        }
                    }
                }
                into(destination)
            }
        } catch (error: Exception) {
            throw GradleException(
                "sherpa-onnx $sherpaVersion: could not extract $sherpaArchiveName: " +
                    (error.message ?: error.javaClass.simpleName),
                error,
            )
        }

        abis.forEach { abi ->
            val dir = destination.resolve(abi)
            if (!dir.isDirectory || dir.listFiles()?.any { it.extension == "so" } != true) {
                throw GradleException(
                    "sherpa-onnx $sherpaVersion: the archive did not contain $abi JNI libraries",
                )
            }
        }
        marker.writeText(sherpaSha256)
        logger.lifecycle("sherpa-onnx $sherpaVersion JNI libraries extracted to $destination")
    }
}

android {
    namespace = "com.example.hermestalk.wakeword"
    compileSdk = 36

    defaultConfig {
        minSdk = 26
    }

    sourceSets["main"].jniLibs.srcDir(sherpaJniDir.get().asFile)

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

tasks.named("preBuild") {
    dependsOn(fetchSherpaOnnxJniLibs)
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.10.2")
}
