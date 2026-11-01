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

## Settings model

The app stores exactly two values:

- relay URL;
- device token.

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

- Version 1 accepts plain HTTP so a loopback relay and an `adb reverse` USB
  device work without certificate setup. The settings screen shows an "insecure
  transport" notice when the URL is `http`.
- Certificate pinning and HTTPS enforcement are a follow-up card.
- The optional stub-relay bearer token is a development fixture, not a
  production authentication scheme.

## Deliberately not in this card

- Foreground service and notification-driven call lifecycle.
- Audio focus management.
- Wake-word detection.
- Attachments and upload queue.
- TLS pinning and certificate management.
- STT/TTS/LLM behaviour (the stub relay sends only a tone).

## CI gate

The public-repository hygiene check scans committed files for personal paths,
LAN addresses, secrets and oversized blobs. The Android unit tests are the
behavioural gate for this module:

```sh
cd android
./gradlew test
```
