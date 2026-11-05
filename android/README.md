# Hermes Talk Android

Kotlin/Compose Android client for Hermes Talk. This module connects to a Hermes
Talk relay over the Pipecat SmallWebRTC/RTVI protocol and exposes a settings
screen plus a visible connection state machine.

## Module layout

- `app` — Compose UI, settings persistence, connection screen, `AppViewModel`.
- `rtvi` — relay endpoint validation, encrypted token storage, connection state
  machine, health probe and the Pipecat SmallWebRTC session wrapper.
- `wakeword` — on-device sherpa-onnx keyword spotting for “Hey Hermes”, the
  false-trigger log and push-to-talk fallback. See
  [`wakeword/README.md`](wakeword/README.md).
- `attachments` — share-target ingest, durable upload queue, relay artifact
  listing/download and the attachment Compose panel.
- `stub-relay` — Python `aiortc` test fixture that implements the RTVI
  signalling surface used by the app.

## Build

Set `ANDROID_HOME` to your local Android SDK. The SDK is not committed.

```sh
export ANDROID_HOME="$HOME/Android/Sdk"
cd android
./gradlew assembleDebug
./gradlew test
```

`./gradlew test` runs the JVM unit tests for endpoint validation, settings
validation, probe mapping, the connection state machine, the call lifecycle,
wake-word policy and the attachment queue. The attachment tests use a real
`com.sun.net.httpserver.HttpServer` bound to loopback, so they exercise HTTP
framing, retries and checksum verification without an emulator. No device is
required.

## Versioning and release signing

`versionName` follows the release tag exactly (`v<versionName>`). `versionCode` is derived in
`app/build.gradle.kts` as `major * 10000 + minor * 100 + patch` and must never be edited by hand; the
two cannot drift. `scripts/check_version.sh` gates both in CI.

The debug build needs no configuration:

```sh
./gradlew assembleDebug
```

A release build needs a keystore and credentials supplied from **outside** this repository. Set them as
environment variables or as Gradle properties in `~/.gradle/gradle.properties`:

| Environment variable | Gradle property |
|---|---|
| `HERMES_TALK_KEYSTORE_FILE` | `hermesTalkKeystoreFile` |
| `HERMES_TALK_KEYSTORE_PASSWORD` | `hermesTalkKeystorePassword` |
| `HERMES_TALK_KEY_ALIAS` | `hermesTalkKeyAlias` |
| `HERMES_TALK_KEY_PASSWORD` | `hermesTalkKeyPassword` |

Create the keystore once (it is never committed):

```sh
keytool -genkeypair -v -keystore ~/.android/hermes-talk-release.jks \
  -alias hermes-talk -keyalg RSA -keysize 4096 -validity 10000
```

Then build and verify:

```sh
HERMES_TALK_KEYSTORE_FILE=~/.android/hermes-talk-release.jks \
HERMES_TALK_KEYSTORE_PASSWORD=... \
HERMES_TALK_KEY_ALIAS=hermes-talk \
HERMES_TALK_KEY_PASSWORD=... \
  ./gradlew assembleRelease
APKSIGNER=$(ls -d "$ANDROID_HOME"/build-tools/*/apksigner | sort -V | tail -1)
"$APKSIGNER" verify --print-certs app/build/outputs/apk/release/app-release.apk
```

`./gradlew assembleRelease` fails before any task executes, with an actionable message naming the
missing values, when the credentials are absent. Debug builds, unit tests and every non-packaging task
keep working without setup.

The keystore and passwords are never committed: `*.jks` and `*.keystore` are gitignored and the
repository hygiene check scans the tree. CI builds debug only (`./gradlew test`, `assembleDebug`, and
the emulator instrumentation job) and needs no signing secret.

## Run the stub relay

From the repository root:

```sh
.venv/bin/python android/stub-relay/relay.py --port 8790
```

The relay binds `127.0.0.1:8790` by default. See
[`stub-relay/README.md`](stub-relay/README.md) for options and limitations.

To connect a USB device or emulator to the host relay:

```sh
adb reverse tcp:8790 tcp:8790
```

Use `http://127.0.0.1:8790` in the app. The loopback default avoids hard-coding a
LAN address. If the relay is started with `--token`, enter the same token in the
app.

## TLS and certificate pinning

The relay terminates TLS with an internal CA (Caddy `tls internal`). Android
does not trust that CA by default, so the app supports two explicit modes:

- **Pinned mode (default for HTTPS).** Fetch the relay certificate from the
  settings screen, compare the visible SPKI pin, certificate fingerprint,
  subject, issuer and expiry, then tap "Use this fingerprint" and save. The app
  pins the leaf SPKI SHA-256 and fails closed on a mismatch. The connection is
  refused and is not silently retried. A matching pin replaces CA trust for the
  probe, but hostname verification is still performed by the HTTP client. There
  is no trust-all `X509TrustManager` anywhere in the tree.
- **Platform-CA mode.** The explicit fallback with no pin. The internal CA must
  be installed in the Android user trust store (Settings → Security →
  Encryption & credentials → Install a certificate → CA certificate) so the
  Pipecat SDK's own OkHttp client, which uses the platform trust manager, can
  reach the relay. The app's network security config includes `user` anchors for
  that reason; a configured pin is still enforced by `TlsTrust` on every probe.

Plain HTTP is allowed only for a loopback URL (`127.0.0.1`, `::1`, `localhost`),
which is the `adb reverse` development path. The settings screen shows an
"Insecure development transport" indicator for it. A non-loopback `http://` URL
is rejected by validation. The network security config denies cleartext except
for those loopback domains.

To run the stub relay with TLS for a local end-to-end check:

```sh
.venv/bin/python android/stub-relay/relay.py \
  --tls-cert android/tls-fixtures/server-a.crt.pem \
  --tls-key android/tls-fixtures/server-a.key.b64
```

Then use `https://127.0.0.1:8790` in the app, fetch the fingerprint, confirm it,
save the pin, and connect. `adb reverse tcp:8790 tcp:8790` still applies.

## Settings model

The app stores four values:

- relay URL;
- device token;
- TLS SPKI pin (`tls_pin`);
- whether to trust the Android CA store instead of pinning
  (`trust_platform_ca`).

They are persisted with `EncryptedSharedPreferences` using an AES256-GCM master
key, AES256-SIV for preference keys and AES256-GCM for values. Nothing else is
persisted. If the encrypted store cannot be opened (for example after an app
reinstall with a stale keystore), reads return null and the error is exposed by
`EncryptedTokenStore.lastError()` instead of crashing startup.

The token is sent only in the `Authorization` header. It is never logged
and is shown masked in the UI unless the user reveals it.

## Connection states

- `Disconnected` — no client is connected.
- `Connecting` — a liveness probe or WebRTC negotiation is running.
- `Connected` — the WebRTC data channel is open.
- `Ready` — the relay sent the RTVI `bot-ready` message.
- `Reconnecting` — a retry is scheduled after a retryable failure.
- `Failed` — the attempt failed; the screen shows the failure category, detail
  and whether it can be retried.

Retryable failures are retried at most three times with 2 s, 5 s and 10 s
backoff. A relay-side backend error is treated as a terminal failure for the
attempt rather than a transport drop. A manual connect starts a fresh attempt.

The Pipecat SDK only logs ICE connection-state changes and never surfaces a
remote hang-up, so the app would otherwise stay in `Connected`/`Ready` forever
when the relay dies. While a session is `Connected` or `Ready`, a liveness
watchdog re-probes `/health` every 15 seconds. Two consecutive transport-level
failures (`Unreachable` or `NotReady`) declare the transport closed, which moves
the UI to `Failed(TransportClosed)` and starts the existing retry backoff.
Responses that prove the relay is still answering HTTP but fail the probe
differently (401/403, or 5xx other than 503) count as alive, so a relay without
a healthy `/health` response is not treated as dead.

## Security notes

- HTTPS is required except on loopback. The probe pins the relay leaf SPKI by
  default and fails closed on a mismatch; the explicit platform-CA mode is the
  only alternative. Plain HTTP on loopback is a development transport and is
  marked as insecure in the UI.
- The optional stub-relay bearer token is a development fixture, not a
  production authentication scheme.

## On-device wake word

The `:wakeword` module adds local keyword spotting for “Hey Hermes”. The
sherpa-onnx KWS model assets live in `app/src/main/assets/wakeword/` and are
committed. The native JNI libraries are not committed: the `:wakeword`
`preBuild` task downloads the pinned sherpa-onnx v2.0.1 Android archive, verifies
its SHA-256 and extracts only the `arm64-v8a` and `x86_64` JNI folders. The first
build therefore needs network access; later builds skip the download when the
extracted directory is complete. The app is limited to those two ABIs.

The wake word runs inside the persistent assistant foreground service rather
than in the activity. When both the persistent assistant and the wake-word
toggle are enabled, `PersistentAssistantService` starts
`AssistantWakeWordRunner`; a detection restores the audio path and launches
`MainActivity` with `ACTION_ASSIST` plus the `EXTRA_WAKE_WORD` extra. The
Settings screen shows the toggle, the current state and the detection and
false-trigger counts.

A live call always wins the microphone. Starting a call suspends the runner,
stops the capture thread and releases `AudioRecord`; ending the call resumes it
only while the persistent assistant and wake word are still enabled. Disabling
the toggle releases the same resources. Push-to-talk is available as a fallback
even when the keyword engine or native library is unavailable.

A foreground service is not a background-activity-launch exemption on Android
14+, so the platform may refuse the `ACTION_ASSIST` launch while the app is not
visible. The runner records the measured latency and any failure; the persistent
notification's content intent is the one-tap fallback. Instrumentation grants
the test process BAL privileges, so the emulator accepts the launch; a stock
device may not. See the placeholder block in
[`docs/TESTING.md`](../docs/TESTING.md#placeholder-real-device-bal-verification)
for the real-device check.

Run the JVM tests with:

```sh
cd android
./gradlew :app:call:testDebugUnitTest :wakeword:test :app:testDebugUnitTest
```

The instrumentation tests cover the model fixtures, permission denial,
push-to-talk, service-owned detection and launch, call suspend/resume, disabling
and a long continuous-listening soak:

```sh
cd android
./gradlew :app:assembleDebugAndroidTest
adb shell am instrument -w \
  -e class com.example.hermestalk.persistent.AssistantWakeWordServiceTest \
  com.example.hermestalk.test/androidx.test.runner.AndroidJUnitRunner
```

The service soak test is annotated `@LargeTest` and defaults to 1800 s; override
the window with `-e wakeWordSoakSeconds <seconds>`. It writes `usage.json` to the
app files directory and logs the summary under the `WakeWordUsage` tag. The
`:wakeword` module also keeps its 60-minute `WakeWordBatteryTest`, overridable
with `-e wakeWordBatteryMinutes 60`. See
[`wakeword/README.md`](wakeword/README.md) for the model provenance, tuning,
sensitivity presets and the false-trigger log.

## Default assistant role and the assist gesture

`MainActivity` registers an exported `android.intent.action.ASSIST` intent filter
with `android.intent.category.DEFAULT` and keeps `launchMode="singleTop"`, so the
system assist gesture (typically a long press of the power button, `KEYCODE_ASSIST`)
opens the call screen. The existing MAIN/LAUNCHER and SEND/SEND_MULTIPLE filters
are unchanged.

The Settings screen shows a "Default assistant" card with the current
`AssistantRoleState` (`Held`, `NotHeld` or `Unavailable`). When the role is not
held, the button requests `android.app.role.ASSISTANT` through
`RoleManager.createRequestRoleIntent`; the system owns the confirmation dialog.
The role is a user-controlled privilege, so the app re-queries
`AssistantRole.state()` on every `ON_RESUME` and after the request dialog
returns. A user can revoke the role from system Settings at any time; revocation
only flips the card back to `NotHeld`. It does not crash the app and does not
restart the foreground call service. The assist gesture follows the current role
holder: when another app holds the role the gesture opens that app, while Hermes
Talk stays reachable through explicit `ACTION_ASSIST` intents and the card
reports `NotHeld`.

The in-app request button is best-effort. The permission controller only shows
the role dialog for requestable roles, and some images mark
`android.app.role.ASSISTANT` as `requestable=false` (the Google-APIs API 34
emulator image does; the value is visible in the permission controller's
`roles.xml`). There the request activity logs "Role is not requestable" and
closes immediately, so the card keeps reporting `NotHeld` and shows a hint that
points at the grant paths below instead of looking like a silent dead end.

Grant paths verified on the API 34 emulator image:

- Settings → Apps → Default apps → Digital assistant app → Hermes Talk → OK
  grants the role. `adb shell settings get secure assistant` then reads
  `com.example.hermestalk/.MainActivity`.
- `adb shell cmd role add-role-holder android.app.role.ASSISTANT com.example.hermestalk`
  grants it from a development machine.

Grant and verify the role from a development machine:

```sh
adb shell cmd role add-role-holder android.app.role.ASSISTANT com.example.hermestalk
adb shell cmd role get-role-holders android.app.role.ASSISTANT
adb shell input keyevent 219
adb shell dumpsys activity activities | grep -i hermestalk
```

`KEYCODE_ASSIST` is 219. Revoke the role and repeat the gesture to confirm the
absence path:

```sh
adb shell cmd role remove-role-holder android.app.role.ASSISTANT com.example.hermestalk
adb shell input keyevent 219
```

The app must still open from an explicit assist intent, with no crash and no
service restart, and the Settings card must report `NotHeld`:

```sh
adb shell cmd role remove-role-holder android.app.role.ASSISTANT com.example.hermestalk
adb shell am start -a android.intent.action.ASSIST -n com.example.hermestalk/.MainActivity
```

The gesture itself follows the role holder, so after revocation it opens the app
that holds the role again.

Measured on the API 34 emulator (`hermes_talk_34`, Google-APIs image):

| Step | Observed |
|---|---|
| `cmd role add-role-holder … com.example.hermestalk` | exit 0; `cmd role get-role-holders` lists `com.example.hermestalk` |
| `settings get secure assistant` after the grant | `com.example.hermestalk/.MainActivity` |
| `input keyevent 219` after the grant | top resumed activity `com.example.hermestalk/.MainActivity`, call screen showing `Call` / `Ready to call` |
| `cmd role remove-role-holder` then `input keyevent 219` | app process alive, zero `FATAL EXCEPTION` entries; the gesture goes to the restored holder |
| in-app request button on this image | permission controller logs `Role is not requestable: android.app.role.ASSISTANT` and returns `RESULT_CANCELED`; the card keeps `NotHeld` and shows the hint |

The instrumentation coverage launches `MainActivity` with an `ACTION_ASSIST`
intent and renders the settings card for all three states:

```sh
cd android
./gradlew :app:connectedDebugAndroidTest \
  -Pandroid.testInstrumentationRunnerArguments.class=com.example.hermestalk.assistant.AssistantRoleTest
```

## Attachments and relay artifacts

The `:attachments` module (namespace
`com.example.hermestalk.attachments`) adds two related flows. It reuses the
`RelaySettings`, `RelayEndpoint`, `TlsTrust`, `SpkiPin` and `tlsPolicy()`
types from `:rtvi`, so uploads and downloads obey exactly the same HTTPS
pinning rules as the connection probe. The token is sent only in the
`Authorization: Bearer <token>` header and is never logged.

### Sharing files into the app

`MainActivity` is the share target for `ACTION_SEND` and
`ACTION_SEND_MULTIPLE` with any MIME type and uses `launchMode="singleTop"`;
a new share while the activity is alive arrives through `onNewIntent` instead
of creating a second activity. The parser accepts `EXTRA_STREAM`, clip-data
URIs and a `content://`/`file://` URI in `EXTRA_TEXT`, ignores duplicates and
rejects blank URIs.

Each shared file is copied into the app's private storage before it is queued:
`filesDir/attachments/queue/<id>/payload.bin`. The display name is reduced to a
safe basename. If the name has no extension, a canonical extension derived from
the declared MIME type is appended (for example `image/png` → `.png`) so the
relay's `extension_matches` check accepts the upload. A single payload is
capped at 50 MiB and an empty file is rejected. The document picker uses
`ACTION_OPEN_DOCUMENT` and persists the read grant where the provider allows it.

### Durable upload queue

The queue index is `filesDir/attachments/queue/index.json`. Writes go to a
temporary file in the same directory, are fsynced and moved into place with
`ATOMIC_MOVE` where the filesystem supports it. A corrupt index is quarantined
as `index.corrupt-<timestamp>.json` and replaced by an empty queue instead of
crashing. Completed records are pruned to the newest 20; unfinished records and
their payloads survive process death and are resumed on the next app start.

Uploads are sequential and single-flight. Each is a multipart `POST
/v1/attachments` with field name `file`, a 15 s connect timeout and a 60 s read
timeout. Progress is reported to the UI about every 32 KiB. The payload is
deleted only after a 201 response; a terminal rejection keeps it so the user can
retry, and an explicit cancel removes it.

Retryable failures (transport errors, timeouts, HTTP 5xx) use a bounded
schedule of 1, 2, 5, 10, 20, 30 and 60 seconds with at most 8 attempts. A relay
`Retry-After` value is honoured as a floor and never shortens the delay. Terminal
rejections (400, 401, 403, 409, 413, 415 and 422) are not retried automatically.
A connectivity callback wakes the worker immediately when the network returns;
queued work also resumes when the app is restarted.

### Artifact listing and download

`GET /v1/artifacts?limit=50` populates the panel. `GET /v1/artifacts/{id}` is
requested with each path segment of the id percent-encoded, written to
`cacheDir/artifacts/`, and verified against the relay's
`X-Artifact-SHA256` header (falling back to the listed checksum). A mismatch
deletes the partial file and reports an error. A verified file is exposed
through the FileProvider and opened with an installed viewer via
`ACTION_VIEW`.

The manifest declares `androidx.core.content.FileProvider` with authority
`${applicationId}.fileprovider`; `res/xml/file_paths.xml` exposes only
`cache/artifacts/` and `files/attachments/`. The provider is not exported and
grants read permission per URI.

### UI and test tags

The panel appears on both the connection screen and the in-call screen. It
exposes `attachment_pick`, `attachment_upload_item`, `attachment_progress`,
`attachment_retry`, `attachment_cancel`, `artifact_refresh`, `artifact_item`,
`artifact_download`, `artifact_open` and `attachment_error`.

The JVM tests live in `attachments/src/test` and cover share parsing, name
sanitisation, ingest limits and hashing, multipart framing and 201 handling,
retry transitions (503 → 201 and terminal 415), mid-body close, the retry
schedule and `Retry-After` floor, queue persistence, corruption quarantine,
pruning, resume, artifact JSON and URL encoding, 404 handling, checksum
verification and the controller's retry budget:

```sh
cd android
./gradlew :attachments:test
```

Known limitations: uploads are sequential rather than parallel; a payload whose
bytes change on disk between ingest and upload is rejected instead of re-hashed;
the artifact panel does not paginate beyond the relay's `limit=50` response; and
opening a downloaded artifact depends on a viewer being installed for its MIME
type.

## Deliberately not in this card

- STT/TTS/LLM behaviour (the stub relay sends only a tone).

## CI gate

The public-repository hygiene check scans committed files for personal paths,
LAN addresses, secrets and oversized blobs. The Android unit tests are the
behavioural gate for this module:

```sh
cd android
./gradlew test
```

The `android (emulator instrumentation)` workflow job boots an API 34 emulator
and runs `:app:connectedDebugAndroidTest` with `androidx.test.filters.LargeTest`
excluded (the wake-word battery run) and the screen-off call window shortened. It
starts `android/stub-relay/relay.py` and forwards port 8790 so the end-to-end
flow can reach the fixture; the retry, token-expiry and call-state tests need
only their in-process loopback server (see
[Regression instrumentation suite](#regression-instrumentation-suite)).

## In-call audio, foreground service and screen-off survival

The in-call feature lives in the `:app:call` Gradle module
(`android/app/call/`, namespace `com.example.hermestalk.call`). It is split so
the lifecycle can be tested without an Android device:

- `CallState.kt`, `CallMachine.kt`, `CallAudioRoutePlanner.kt`,
  `CallLevelMeter.kt` and `CallTimer.kt` are pure JVM code. The call machine is a
  deterministic reducer: events such as permission results, session states,
  audio-focus changes, route changes, mute and end requests produce a new state
  plus a list of side effects.
- `AudioFocusController.kt`, `AudioRouteController.kt`,
  `MicActivityMonitor.kt`, `CallForegroundController.kt` and
  `BatteryOptimisation.kt` are the injectable Android seams. The production
  implementations wrap `AudioManager`, `AudioRecordingConfiguration` callbacks,
  the foreground service and the battery-optimisation settings intent.
- `CallController.kt` / `CallControllers` orchestrates the machine, the RTVI
  session (`CallSession` / `RtviCallSession`), the foreground service, audio
  focus, routing, the microphone monitor, the level meter and the timer.
- `CallForegroundService.kt` owns the microphone foreground service, a partial
  wakelock, a high-performance Wi-Fi lock, and notification actions for mute and
  end. It is declared with `foregroundServiceType="microphone"` and
  `stopWithTask="true"`, so swiping the task away ends the call and releases the
  microphone.
- `ui/CallScreen.kt` is a stateless Compose screen. It renders the call status,
  the microphone visualiser, mute/end controls, available audio routes, the
  microphone-permission card and the battery-optimisation prompt. Test tags
  include `call_visualiser`, `call_mute`, `call_end`,
  `call_permission_denied` and `call_battery_prompt`.

The call states are `Idle`, `AwaitingMicPermission`,
`Starting(ServiceStart)`, `Starting(Connecting)`, `Active`, `Reconnecting`,
`Ending`, `Ended` and `Failed`. `Failed` carries a stable reason such as
`MicrophonePermissionDenied`, `RelayUnreachable`, `RelayRejected`,
`HandshakeFailed`, `AudioFocusLost` or `Unexpected`. A denied microphone
permission fails before the service starts. Any audio-focus loss (permanent or
transient) mutes the microphone and clears the focus flag without tearing down
the session or ending the call; focus is re-asserted only when the user unmutes
or the session reconnects. The service is stopped, the session is
disconnected, audio focus is abandoned, the communication device and audio mode
are restored, and the timer is cancelled on every terminal transition.

The foreground service uses the `microphone` type because Android 14 requires a
declared type for every foreground service and the call records from the
microphone. The matching `FOREGROUND_SERVICE_MICROPHONE` permission and
`RECORD_AUDIO` runtime permission are declared in the app manifest. The partial
wakelock and Wi-Fi lock keep the CPU and network alive while the screen is off;
the notification stays ongoing and exposes mute/end actions so the call can be
controlled without reopening the app.

### Instrumentation checks

The JVM tests cover the machine, route planner, level meter, timer and the
controller with fake Android seams:

```sh
cd android
./gradlew :app:call:testDebugUnitTest :app:testDebugUnitTest
```

The device tests need a reachable stub relay and a USB device or emulator. From
the repository root:

```sh
adb reverse tcp:8790 tcp:8790
.venv/bin/python android/stub-relay/relay.py --port 8790
```

Then run the instrumentation suite:

```sh
cd android
./gradlew :app:connectedDebugAndroidTest
```

`CallForegroundServiceTest` starts a call, verifies the microphone foreground
service and its notification actions, turns the screen off with UiDevice and
checks that the call timer keeps advancing, then ends the call and verifies that
the service stopped and the microphone was released. The screen-off duration
defaults to 600 seconds and can be overridden:

```sh
adb shell am instrument -w \
  -e class com.example.hermestalk.call.CallForegroundServiceTest \
  -e screenOffSeconds 600 \
  com.example.hermestalk.test/androidx.test.runner.AndroidJUnitRunner
```

`CallPermissionTest` grants `RECORD_AUDIO`, starts a call and then feeds a
denied permission result through `CallController.onMicPermissionResult` — the
same path the activity's permission launcher takes — and verifies that the call
fails with `MicrophonePermissionDenied`, the foreground service never keeps
running, the audio mode is restored and the controller does not crash.
Revoking the permission with `UiAutomation` is avoided because it kills the
instrumented process, since the test runner lives in the app process. When the
relay is unreachable, `CallForegroundServiceTest` instead verifies that the call
fails cleanly and the service is stopped.

Known limitations: the microphone visualiser is driven by the session's reported
input level and the platform recording callback, not by raw PCM capture; the
screen-off check depends on the stub relay staying reachable through `adb
reverse`; and the notification actions require the app process to be alive,
which the foreground service guarantees during a call. A physical headset switch
cannot be simulated reliably on the emulator, so the "no audio drop" behaviour is
covered at the reducer level: `AudioDevicesChanged` re-plans the route and emits
only `ApplyAudioRoute`, never `DisconnectAndReleaseSession`, and the session is
left connected. Plugging or unplugging a headset on a real device exercises the
same callback through `AudioManager.registerAudioDeviceCallback`.

### Regression instrumentation suite

`android/app/src/androidTest` carries the client regression suite. Every test
binds loopback only; nothing depends on a LAN address and no personal data is
committed.

- `call/CallStateTest` starts a call against a closed loopback port and verifies
  that the call fails with `RelayUnreachable` and that the foreground service,
  the microphone and the audio mode are released. It also checks that mute and
  end are ignored while no call is live. A call fails on the first unreachable
  probe by design: cleanup is the call-level contract, and the retry budget
  belongs to the connection screen.
- `call/CallPermissionTest` covers the denied microphone permission (see above)
  and `call/CallScreenTest` renders the permission card, so a denial produces a
  visible state rather than a crash.
- `attachments/UploadRetryTest` drives the real `UploadQueueController` and
  `HttpUploadEngine` against an in-process HTTP fixture: two 503 responses then
  a 201 with the 1 s and 2 s backoff observed, a terminal 401 that keeps the
  payload for a manual retry, and a persisted upload that resumes after a
  process restart.
- `attachments/TokenExpiryTest` exercises an expired bearer credential on the
  liveness probe, the session handshake, the upload engine and the artifact
  client. Every surface must fail as `Unauthorized` without retrying and without
  leaking the token into the UI detail.
- `support/LoopbackStubRelay` is the fixture those tests use. It is a small
  HTTP/1.1 server on `127.0.0.1` inside the app process, with scripted upload
  statuses, a bearer-token check, artifact listing and download, and a request
  log. Because it needs no host relay and no `adb reverse`, these tests run on a
  bare emulator in CI.
- `e2e/StubRelayFlowTest` drives the real activity through Compose test tags:
  it seeds the encrypted settings store, connects to the Python stub relay,
  waits for `Ready`, starts a call, toggles mute, ends the call and checks that
  the service and audio mode are released. It also covers the expired-token
  screen. The relay-backed test skips with a clear message when
  `127.0.0.1:8790` does not answer, so a local run without the fixture stays
  green.

The settings screen exposes `settings_relay_url`, `settings_token` and
`settings_connect` for these flows.

Run the self-contained subset (no host relay, suitable for CI):

```sh
cd android
./gradlew :app:connectedDebugAndroidTest \
  -Pandroid.testInstrumentationRunnerArguments.class=com.example.hermestalk.attachments.UploadRetryTest,com.example.hermestalk.attachments.TokenExpiryTest,com.example.hermestalk.call.CallStateTest
```

Run the full suite, including the relay-backed flow, with shortened long tests:

```sh
adb reverse tcp:8790 tcp:8790
.venv/bin/python android/stub-relay/relay.py --port 8790
cd android
./gradlew :app:connectedDebugAndroidTest \
  -Pandroid.testInstrumentationRunnerArguments.screenOffSeconds=15 \
  -Pandroid.testInstrumentationRunnerArguments.wakeWordBatteryMinutes=1
```

`@LargeTest` marks the wake-word soak, which defaults to 1800 s, and the
container E2E call. CI runs `scripts/android-instrumentation.sh`: it grants
`android.app.role.ASSISTANT` through the shell (the role gate cannot use the
user dialog headless), runs the default suite with `screenOffSeconds=15`, and
then runs the `@LargeTest` soak explicitly with `wakeWordSoakSeconds=120`. A
verifier reads the JUnit XML and fails on a skipped or absent case. The
container E2E call runs in its own job via `scripts/e2e-emulator-container.sh`.

## Always-on assistant service: screen-off, task swipe and reboot

The persistent assistant lives in `:app:call` next to the call service:
`PersistentAssistantService.kt` (the service), `PersistentAssistant.kt`
(policy, process-wide state and the controller), `PersistentAssistantSettingsStore.kt`
(the plain opt-in store) and `BootReceiver.kt` (BOOT_COMPLETED and
MY_PACKAGE_REPLACED). It is separate from `CallForegroundService`, which remains
call-scoped and unchanged.

### Manifest facts

- The service declares `foregroundServiceType="microphone|specialUse"`,
  `stopWithTask="false"` and `exported="false"`, with
  `PROPERTY_SPECIAL_USE_FGS_SUBTYPE` describing always-on on-device wake-word
  listening for the local assistant.
- The app declares `RECEIVE_BOOT_COMPLETED` and
  `FOREGROUND_SERVICE_SPECIAL_USE` in addition to the existing microphone
  foreground-service permission.
- The boot receiver is `exported="true"` with both `BOOT_COMPLETED` and
  `MY_PACKAGE_REPLACED` filters. `LOCKED_BOOT_COMPLETED` is not handled because
  the opt-in flag lives in credential-protected plain SharedPreferences
  (`hermes_talk_persistent`, key `assistant_enabled`, default off).

The service returns `START_STICKY`, enters the foreground on every
`onStartCommand`, keeps running in `onTaskRemoved`, and releases its partial wake
lock in `onDestroy`. The wake lock (`hermes_talk:assistant-listening`) is
non-reference-counted, has no timeout and is held only while the wake-word
controller is in `WakeWordState.Listening`; it is released for every other state
and while a call is live.

### Android 14 microphone foreground-service rule

Apps targeting API 34+ cannot start a **microphone** foreground service from a
`BOOT_COMPLETED` receiver; the system throws
`ForegroundServiceStartNotAllowedException`. The documented fallback is to start
with the `specialUse` type when the microphone type is unavailable and promote
to `microphone` once the app is visible
(see `developer.android.com/about/versions/15/behavior-changes-15#fgs-boot-completed`
and `.../fgs/restrictions-bg-start#wiu-restrictions`).

This implementation follows that rule exactly:

1. When `RECORD_AUDIO` is granted on API 29+, the service first calls
   `startForeground(id, notification, FOREGROUND_SERVICE_TYPE_MICROPHONE)`.
2. On `ForegroundServiceStartNotAllowedException`, `SecurityException` or
   `IllegalStateException` it records the exception class and message in
   `PersistentAssistant.state.micRefusal`, logs it with `Log.w`, and retries
   synchronously with `FOREGROUND_SERVICE_TYPE_SPECIAL_USE` so it never misses
   the 5-second startForeground window.
3. Without `RECORD_AUDIO`, or on API < 29, it goes straight to `specialUse`
   (plain `startForeground` on API < 29).
4. `ACTION_PROMOTE_MIC` is sent from the app's `ON_RESUME`; a successful retry
   clears the refusal and updates the notification text.

The Settings card shows `Running (microphone)`, `Running (special use)`,
`Listening` or `Not running`, and displays the refusal hint with the recorded
exception when `micRefusal` is set. Test tags are `settings_persistent_toggle`,
`settings_persistent_status`, `settings_persistent_mic_refused` and
`settings_persistent_battery`.

### Battery exemption

The Settings card reuses the existing `BatteryOptimisation` interface from
`:app:call`. When the app is not exempt, it shows **Allow background activity**,
which opens the system exemption UI. Battery optimisation must be off for the
assistant to survive Doze; verify or change the whitelist from a development
machine:

```sh
adb shell dumpsys deviceidle whitelist +com.example.hermestalk
adb shell dumpsys deviceidle whitelist | grep hermestalk
adb shell dumpsys deviceidle whitelist -com.example.hermestalk
```

### Instrumentation checks

`PersistentAssistantServiceTest` covers the empirical rule and the soak:

```sh
adb shell am instrument -w \
  -e class com.example.hermestalk.persistent.PersistentAssistantServiceTest \
  -e persistentSoakSeconds 600 \
  com.example.hermestalk.test/androidx.test.runner.AndroidJUnitRunner
```

`backgroundStartUsesThePlatformAllowedForegroundType` presses Home, starts the
service and asserts the type the platform actually allowed: `SpecialUse` with a
non-null refusal when the API 34 background rule fires, or `Microphone` when
`am instrument` grants the process background-activity-start privileges. The
dumpsys check accepts either the symbolic name or the API 34 bit mask.
Instrumentation's privilege can mask the real refusal on an emulator, so the
deterministic microphone→specialUse fallback is covered by
`PersistentAssistantPolicyTest.fallbackUsesSpecialUseAfterAMicrophoneRefusal`.
The foreground test asserts the microphone type and the listening-scoped wake
lock. The soak test turns the screen off, forces Doze for
`persistentSoakSeconds` (default 600), and verifies the same pid and microphone
type before unforcing Doze and waking the screen.

### Measured boot behaviour on the archive AVD (API 34)

The refusal is not an assumption: it was reproduced on `hermes_talk_34` with
`adb reboot` and the opt-in flag enabled. The boot receiver started the service,
and logcat recorded the platform refusing the microphone type:

```
W ActivityManager: Foreground service started from background can not have
  location/camera/microphone access: service
  com.example.hermestalk/.call.PersistentAssistantService
W PersistentAssistant: Microphone foreground type refused
  (java.lang.SecurityException: Starting FGS with type microphone …
  targetSDK=34 … and the app must be in the eligible state/exemptions to
  access the foreground only permission); falling back
```

The platform raised `SecurityException` here, not the
`ForegroundServiceStartNotAllowedException` the Android 15 release notes name;
the service catches both. `dumpsys activity services` then reported
`isForeground=true … types=40000000` (specialUse), `startCommandResult=1`
(START_STICKY), `createdFromFg=false`, `allowStartForeground=BOOT_COMPLETED`,
`uidState: RCVR`, and `mAllowWhileInUsePermissionInFgsReason=DENIED` — the
service came back with no user launch. Launching `MainActivity` promoted it to
`types=00000080` (microphone) with
`mAllowWhileInUsePermissionInFgsReason=PROC_STATE_TOP`. A
`run-as … kill -9` of the process was followed by a sticky restart (new pid,
specialUse again while backgrounded). Measured soak, swipe, reboot and battery
numbers are in `docs/TESTING.md`.
