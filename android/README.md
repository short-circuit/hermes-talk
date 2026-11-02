# Hermes Talk Android

Kotlin/Compose Android client for Hermes Talk. This module connects to a Hermes
Talk relay over the Pipecat SmallWebRTC/RTVI protocol and exposes a settings
screen plus a visible connection state machine.

## Module layout

- `app` — Compose UI, settings persistence, connection screen, `AppViewModel`.
- `rtvi` — relay endpoint validation, encrypted token storage, connection state
  machine, health probe and the Pipecat SmallWebRTC session wrapper.
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
validation, probe mapping and the connection state machine. No emulator or
device is required.

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

## Deliberately not in this card

- Foreground service and notification-driven call lifecycle.
- Audio focus management.
- Wake-word detection.
- Attachments and upload queue.
- STT/TTS/LLM behaviour (the stub relay sends only a tone).

## CI gate

The public-repository hygiene check scans committed files for personal paths,
LAN addresses, secrets and oversized blobs. The Android unit tests are the
behavioural gate for this module:

```sh
cd android
./gradlew test
```

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
permission fails before the service starts. Losing audio focus mutes the
microphone without tearing down the session; a permanent focus loss also
releases focus and marks the call failed. The service is stopped, the session is
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

`CallPermissionTest` revokes `RECORD_AUDIO`, starts a call and verifies that the
call fails with `MicrophonePermissionDenied` without starting the foreground
service. When the relay is unreachable, `CallForegroundServiceTest` instead
verifies that the call fails cleanly and the service is stopped.

Known limitations: the microphone visualiser is driven by the session's reported
input level and the platform recording callback, not by raw PCM capture; the
screen-off check depends on the stub relay staying reachable through `adb
reverse`; and the notification actions require the app process to be alive,
which the foreground service guarantees during a call.
