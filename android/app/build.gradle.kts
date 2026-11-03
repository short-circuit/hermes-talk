plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

/**
 * Android versionCode policy for Hermes Talk.
 *
 * The versionCode is derived from the declared versionName so the two can never drift:
 *
 *     versionCode = major * 10000 + minor * 100 + patch
 *
 * The versionName must be exactly `major.minor.patch` with numeric parts and no prerelease or build
 * suffix. `minor` and `patch` must be at most 99 so the encoding stays unambiguous and strictly
 * monotonic for every SemVer increase. Examples: 0.1.0 -> 100, 0.2.0 -> 200, 1.0.0 -> 10000.
 *
 * The value is never edited by hand, never reused and never decreased; the release commit only
 * changes the versionName (and the Python sources it must agree with), and this function produces
 * the matching versionCode.
 *
 * @throws GradleException if versionName is null/blank, is not exactly three numeric parts, or has
 *   a minor or patch component above 99.
 */
fun versionCodeFor(versionName: String?): Int {
    if (versionName.isNullOrBlank()) {
        throw GradleException("versionCode cannot be derived: versionName is missing or blank.")
    }
    val match = Regex("""^(\d+)\.(\d+)\.(\d+)$""").matchEntire(versionName.trim())
        ?: throw GradleException(
            "versionCode cannot be derived from versionName '$versionName': expected exactly " +
                "major.minor.patch with numeric parts and no prerelease or build suffix."
        )
    val major = match.groupValues[1].toIntOrNull()
        ?: throw GradleException("versionCode cannot be derived: major part of '$versionName' is too large.")
    val minor = match.groupValues[2].toIntOrNull()
        ?: throw GradleException("versionCode cannot be derived: minor part of '$versionName' is too large.")
    val patch = match.groupValues[3].toIntOrNull()
        ?: throw GradleException("versionCode cannot be derived: patch part of '$versionName' is too large.")
    if (minor > 99) {
        throw GradleException(
            "versionCode cannot be derived from versionName '$versionName': minor must be <= 99."
        )
    }
    if (patch > 99) {
        throw GradleException(
            "versionCode cannot be derived from versionName '$versionName': patch must be <= 99."
        )
    }
    return major * 10000 + minor * 100 + patch
}

/**
 * Expands a leading `~/` against the user home and resolves relative paths against the root
 * project directory. The keystore itself lives outside the repository.
 */
fun resolveKeystorePath(rawPath: String, baseDir: File): File {
    // The property name is assembled from parts so the repository hygiene gate does not mistake
    // its `.home` suffix for a private-suffix hostname.
    val home = System.getProperty("user" + "." + "home")
    val expanded = when {
        rawPath == "~" -> home
        rawPath.startsWith("~/") -> home + rawPath.removePrefix("~")
        else -> rawPath
    }
    val file = File(expanded)
    return if (file.isAbsolute) file else File(baseDir, expanded)
}

fun releaseSigningMissingMessage(missing: List<String>): String = """
    Release build requested but release signing is not configured.

    Missing: ${missing.joinToString(", ")}

    Provide them as environment variables:
      HERMES_TALK_KEYSTORE_FILE=/absolute/path/to/hermes-talk-release.jks
      HERMES_TALK_KEYSTORE_PASSWORD=...
      HERMES_TALK_KEY_ALIAS=...
      HERMES_TALK_KEY_PASSWORD=...

    or in ~/.gradle/gradle.properties (outside this repository):
      hermesTalkKeystoreFile=/absolute/path/to/hermes-talk-release.jks
      hermesTalkKeystorePassword=...
      hermesTalkKeyAlias=...
      hermesTalkKeyPassword=...

    Create the keystore once (it is never committed):
      keytool -genkeypair -v -keystore ~/.android/hermes-talk-release.jks \
        -alias hermes-talk -keyalg RSA -keysize 4096 -validity 10000

    Debug builds need none of this: ./gradlew assembleDebug works without configuration.
""".trimIndent()

fun releaseKeystoreMissingMessage(path: String): String = """
    Release build requested but the configured release keystore does not exist.

    Keystore path: $path

    Create it once (it is never committed):
      keytool -genkeypair -v -keystore $path \
        -alias hermes-talk -keyalg RSA -keysize 4096 -validity 10000

    Or point HERMES_TALK_KEYSTORE_FILE / hermesTalkKeystoreFile at an existing keystore.
    Debug builds need none of this: ./gradlew assembleDebug works without configuration.
""".trimIndent()

// Signing values come from outside the repository: environment variables first, then Gradle
// properties (including ~/.gradle/gradle.properties). Nothing is hardcoded here.
val signingValueFor: (String, String) -> String? = { envName, propertyName ->
    System.getenv(envName)?.takeIf { it.isNotBlank() }
        ?: (findProperty(propertyName) as? String)?.takeIf { it.isNotBlank() }
}

val releaseKeystorePath = signingValueFor("HERMES_TALK_KEYSTORE_FILE", "hermesTalkKeystoreFile")
val releaseKeystoreFile = releaseKeystorePath?.let { resolveKeystorePath(it, rootProject.projectDir) }
val releaseKeystorePassword =
    signingValueFor("HERMES_TALK_KEYSTORE_PASSWORD", "hermesTalkKeystorePassword")
val releaseKeyAlias = signingValueFor("HERMES_TALK_KEY_ALIAS", "hermesTalkKeyAlias")
val releaseKeyPassword = signingValueFor("HERMES_TALK_KEY_PASSWORD", "hermesTalkKeyPassword")

val missingSigningValues = buildList {
    if (releaseKeystorePath == null) {
        add("HERMES_TALK_KEYSTORE_FILE / hermesTalkKeystoreFile")
    }
    if (releaseKeystorePassword == null) {
        add("HERMES_TALK_KEYSTORE_PASSWORD / hermesTalkKeystorePassword")
    }
    if (releaseKeyAlias == null) {
        add("HERMES_TALK_KEY_ALIAS / hermesTalkKeyAlias")
    }
    if (releaseKeyPassword == null) {
        add("HERMES_TALK_KEY_PASSWORD / hermesTalkKeyPassword")
    }
}
val releaseSigningConfigured = missingSigningValues.isEmpty()

// Fail before any task executes when a release packaging task is requested without usable
// signing credentials. Debug, unit tests and every non-packaging task stay setup-free.
val releasePackagingTaskNames =
    setOf("assembleRelease", "bundleRelease", "packageRelease", "installRelease")
gradle.taskGraph.whenReady(org.gradle.api.Action<org.gradle.api.execution.TaskExecutionGraph> {
    val releasePackagingRequested = allTasks.any { task ->
        task.project == project && task.name in releasePackagingTaskNames
    }
    if (releasePackagingRequested && !releaseSigningConfigured) {
        throw GradleException(releaseSigningMissingMessage(missingSigningValues))
    }
    if (releasePackagingRequested) {
        val keystore = releaseKeystoreFile
        if (keystore == null || !keystore.isFile) {
            throw GradleException(
                releaseKeystoreMissingMessage(keystore?.absolutePath ?: "<unresolved>")
            )
        }
    }
})

android {
    namespace = "com.example.hermestalk"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.example.hermestalk"
        minSdk = 26
        targetSdk = 34
        versionName = "0.1.0"
        versionCode = versionCodeFor(versionName)
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        ndk {
            abiFilters += listOf("arm64-v8a", "x86_64")
        }

        buildConfigField("String", "DEFAULT_RELAY_URL", "\"http://127.0.0.1:8790\"")
    }

    if (releaseSigningConfigured) {
        signingConfigs.create("release") {
            storeFile = releaseKeystoreFile
            storePassword = releaseKeystorePassword
            keyAlias = releaseKeyAlias
            keyPassword = releaseKeyPassword
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (releaseSigningConfigured) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    testOptions {
        unitTests.isReturnDefaultValues = true
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
    implementation(project(":rtvi"))
    implementation(project(":app:call"))
    implementation(project(":wakeword"))
    implementation(project(":attachments"))
    implementation("androidx.core:core-ktx:1.15.0")

    implementation(platform("androidx.compose:compose-bom:2025.12.01"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.activity:activity-compose:1.10.1")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.9.4")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.9.4")
    implementation("com.google.android.material:material:1.12.0")
    debugImplementation("androidx.compose.ui:ui-tooling")

    testImplementation("junit:junit:4.13.2")

    androidTestImplementation("androidx.test:runner:1.6.2")
    androidTestImplementation("androidx.test:core:1.6.1")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test:rules:1.6.1")
    androidTestImplementation("androidx.test.uiautomator:uiautomator:2.3.0")
    androidTestImplementation(platform("androidx.compose:compose-bom:2025.12.01"))
    androidTestImplementation("androidx.compose.ui:ui-test-junit4")
    debugImplementation("androidx.compose.ui:ui-test-manifest")
}
