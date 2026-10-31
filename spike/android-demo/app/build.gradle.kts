plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.example.hermestalk.spike"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.example.hermestalk.spike"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.0.1-spike"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
        // Probe build: links the Pipecat Android transport so its AAR, native libraries and
        // Kotlin metadata are exercised against this toolchain. The default debug build stays
        // dependency-free on purpose.
        create("probe") {
            initWith(getByName("debug"))
            applicationIdSuffix = ".probe"
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    packaging {
        resources.excludes += setOf("META-INF/*.kotlin_module")
    }
}
kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    // Probe build only: the transport under evaluation. The default build stays dependency-free.
    "probeImplementation"("ai.pipecat:small-webrtc-transport:1.2.2")
}
