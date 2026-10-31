# Android client stack: transport, wake word, and background execution

> Status: **spike result, 2026-10-31.** Platform statements are cited from the Android developer
> documentation; every measurement below names the device and the command that produced it. The
> spike app lives in `spike/android-demo/` and is disposable — the real client is `android/app/`.
> This document confirms ADR 0002 and adds the execution rules that ADR does not cover.

## Recommendation

| Area | Choice | Basis |
|---|---|---|
| Transport | Pipecat RTVI client, `ai.pipecat:small-webrtc-transport` | No SFU, no token server, BSD-2; matches single-user LAN deployment |
| Fallback transport | LiveKit `livekit-android` + self-hosted SFU | Only if multi-device, telephony, or off-LAN peers become requirements |
| Wake word | sherpa-onnx open-vocabulary KWS, `sherpa-onnx-kws-zipformer-zh-en-3M` int8, chunk-8 | Apache-2.0, on-device, keywords editable without retraining |
| Call lifecycle | Foreground service, `foregroundServiceType="microphone|mediaPlayback"`, partial wake lock, battery-optimisation exemption | Required by Android 14; measured to survive a 10-minute screen-off session |
| Audio routing | `AudioManager.setCommunicationDevice()` (API 31+), plus per-device choice of `USAGE_MEDIA` vs `USAGE_VOICE_COMMUNICATION` | Android 13+ deprecates `startBluetoothSco()`; see the audio section |

Do not build a WebRTC client that assumes the framework will pick a working output: on the test
device `USAGE_VOICE_COMMUNICATION` + `MODE_IN_COMMUNICATION` produced a track that never became
active, while `USAGE_MEDIA` + `MODE_NORMAL` rendered in real time (measurements below).

## Transport comparison

Artifact facts are read from Maven Central metadata; behaviour facts from the vendor documentation.

| | Pipecat SmallWebRTC | LiveKit Android + starter app |
|---|---|---|
| Client artifact | `ai.pipecat:small-webrtc-transport:1.2.2` (latest, 2026-10-08), 21.9 MB AAR bundling libwebrtc | `io.livekit:livekit-android:2.18.2`, 3.4 MB AAR |
| Transitive weight | `ai.pipecat:client`, kotlinx-serialization, Ktor client 2.3.5, core-ktx 1.17 | `io.github.webrtc-sdk:android-prefixed:125.6422.07` (20.3 MB AAR), OkHttp, `audioswitch` (JitPack), protobuf-javalite, dagger, jain-sip |
| Licence | BSD-2 plus bundled libwebrtc/third-party licences | Apache-2.0; starter app MIT |
| Server side | One Pipecat process exposing `/start` or `/api/offer`; direct peer-to-peer, no SFU | LiveKit SFU (`livekit-server`), token endpoint, TURN for NAT traversal |
| Auth | None built in; the relay is the trust boundary | Signed JWT per participant from a token server |
| Connect API | `PipecatClientSmallWebRTC(SmallWebRTCTransport(context), PipecatClientOptions(enableMic = true))`, then `startBotAndConnect(APIRequest(endpoint = ...))` | `Room.connect(url, token)` |
| ICE / TURN | `IceConfig` accepted by the transport | Server-issued ICE config |
| Events / barge-in | RTVI: bot-ready, transcripts, interruptions | Data channels + agent framework |
| Audio device control | `AudioDevices.Speakerphone` / `AudioDevices.Earpiece` only; no Bluetooth selector in the transport API | `audioswitch` handles SCO/LEA headset selection |
| Reconnect / multi-party | Single peer; reconnect is ours to implement | Built-in reconnection, participants, room semantics |
| Extra processes to operate | None beyond the relay | SFU + token server (+ TURN off-LAN) |

**Why Pipecat for v1:** the deployment is one user, one phone, one relay on a LAN. LiveKit's SFU and
token endpoint add two long-lived processes and a JWT path that buys nothing at this scale, and the
starter app defaults to LiveKit Cloud, so it must be re-pointed at a self-hosted server anyway
(`TokenExt.kt`, development token server or own endpoint). Pipecat's transport is one dependency and
one `/api/offer` endpoint the relay already owns.

**Why LiveKit stays on the table:** it is the only option of the two with a maintained
Bluetooth/LE audio switcher, reconnection, and room semantics. If the phone must also talk to a
second device, or if calls leave the LAN (tailnet, cellular), migrate — the transport seam below is
deliberately small enough that migration does not touch the audio or service code.

**Risk to verify first in the real client:** the Pipecat transport owns microphone capture and
playback (`enableMic`), which means the app does not control `AudioUsage`/`AudioMode` directly.
Given the measurement below, confirm on the target phone that the SDK's communication-mode audio
path actually renders; if it does not, the app must set the communication device and mode itself
before connecting.

## Required permission manifest

This is the minimum set for a wake-word-plus-call client on Android 14. Every entry is justified;
drop nothing without checking the matching platform rule.

```xml
<!-- Capture: the call and the wake word both need the microphone. -->
<uses-permission android:name="android.permission.RECORD_AUDIO" />

<!-- Foreground service: one service type per thing it does. -->
<uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_MICROPHONE" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_MEDIA_PLAYBACK" />
<uses-permission android:name="android.permission.POST_NOTIFICATIONS" />

<!-- Call audio: communication mode, routing, and keeping the CPU awake while the screen is off. -->
<uses-permission android:name="android.permission.MODIFY_AUDIO_SETTINGS" />
<uses-permission android:name="android.permission.WAKE_LOCK" />
<uses-permission android:name="android.permission.BLUETOOTH_CONNECT" />

<!-- Relay on the LAN. -->
<uses-permission android:name="android.permission.INTERNET" />
<uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />

<!-- Doze exemption so the relay socket survives a screen-off session. -->
<uses-permission android:name="android.permission.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS" />

<uses-feature android:name="android.hardware.microphone" android:required="true" />
<!-- the spike declared required="false" only because it ran on a TV box with no capture device -->

<application ...>
    <service
        android:name=".CallService"
        android:exported="false"
        android:foregroundServiceType="microphone|mediaPlayback" />
</application>
```

Notes that cost time if missed:

- `RECORD_AUDIO` must be **granted** before `startForeground()` is called with the microphone type,
  otherwise the call throws `SecurityException`. Request it, then start.
- `POST_NOTIFICATIONS` (API 33+) is not required for the service to run, but without it the ongoing
  notification is invisible, which makes the service look like a leak.
- `BLUETOOTH_CONNECT` (API 31+) is a runtime permission; without it the app cannot enumerate or
  select Bluetooth devices.
- `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` is a normal permission, but the exemption itself is a user
  dialog (`ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`); Play policy expects it only for apps whose
  core function needs it — a voice assistant qualifies, an attachment viewer does not.

## Android 14 foreground-service rules

Source: [Foreground service types](https://developer.android.com/develop/background-work/services/fgs/service-types)
and [Restrictions on starting a foreground service from the background](https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start).

- A `microphone` foreground service requires `FOREGROUND_SERVICE_MICROPHONE` **and** a granted
  `RECORD_AUDIO`, and passes `FOREGROUND_SERVICE_TYPE_MICROPHONE` to `startForeground()`.
- A microphone service **cannot be started while the app is in the background**, and **cannot be
  launched from a `BOOT_COMPLETED` receiver** (a small set of exemptions applies: system alert
  window, companion-device association, and a few privileged cases). Practical consequence: start
  the call from visible UI or from a notification action, and on boot only start a
  `mediaPlayback`-type service — or defer the microphone start until the user opens the app.
- The same while-in-use restriction applies to `camera` and `location`; audio playback is not
  restricted, which is why `mediaPlayback` can be started from more places.
- `targetSdk` 34 requires the declared type to match what the service actually uses; declaring a
  type you do not use is allowed, using a type you did not declare is not.

## Doze, wake locks, and the battery exemption

Source: [Optimize for Doze and App Standby](https://developer.android.com/training/monitoring-device-state/doze-standby).

- Doze suspends network access and ignores wake locks for apps that are not exempt. A foreground
  service alone does not exempt an app from Doze.
- Apps on the battery-optimisation exemption list are **partially exempt**: they can use the network
  and hold partial wake locks during Doze and App Standby. That is the mechanism a screen-off call
  needs; `PowerManager.isIgnoringBatteryOptimizations()` reports the state, and the demo shows it in
  its status line.
- Hold a `PARTIAL_WAKE_LOCK` for the duration of capture work. It does not stop Doze, but it keeps
  the CPU running so the capture thread is not frozen between maintenance windows.
- Do not rely on alarms or job scheduling for anything inside the call; keep the socket alive and let
  the transport reconnect if the link drops.

## Audio routing and Bluetooth headsets

Sources: [AudioManager self-managed call guide](https://developer.android.com/develop/connectivity/bluetooth/ble-audio/audio-manager),
[Audio routing API updates in Android 14 for VoIP apps](https://developer.android.com/develop/connectivity/telecom/voip-app/api-updates).

- `startBluetoothSco()`/`stopBluetoothSco()` are deprecated; from Android 13 the supported path is
  `AudioManager.setCommunicationDevice(AudioDeviceInfo)`, and `getCommunicationDevice()` replaces
  `isBluetoothScoOn()`. Android 14 additionally exposes hearing aids as selectable devices and
  deprecates further routing APIs.
- The canonical sequence is: set `MODE_IN_COMMUNICATION`, request audio focus, enumerate
  `availableCommunicationDevices`, call `setCommunicationDevice()` with the headset, then wait for
  `communicationDevice` to become that device (the call returns a boolean immediately; the route is
  confirmed asynchronously).
- Google recommends the Telecom Jetpack library for VoIP apps that can adopt it; a plain
  `AudioManager` path is acceptable for a single-purpose assistant.
- Permissions: `MODIFY_AUDIO_SETTINGS` for mode changes, `BLUETOOTH_CONNECT` (runtime, API 31+) to
  see devices. `TYPE_BLUETOOTH_SCO` and `TYPE_BLE_HEADSET` are the device types to match; wired and
  USB headsets are worth accepting as well.
- The Pipecat transport does not expose a Bluetooth selector (`Speakerphone`/`Earpiece` only), so the
  client must call `setCommunicationDevice()` itself after the transport starts, or the SDK must be
  wrapped. Budget a small adapter for this; do not assume the SDK routes to a headset.

## Wake-word engine

Recommendation: **sherpa-onnx open-vocabulary keyword spotting**, Apache-2.0, fully on-device.

| Fact | Value |
|---|---|
| Model | `sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20` (Chinese + English) |
| Size | int8 encoder 4.4 MB, decoder 743 KB, joiner 85 KB; `en.phone` 3.2 MB only for phoneme-based keyword files |
| Latency | chunk-8 export: 160 ms per decision; chunk-16: 320 ms (lower latency, lower accuracy) |
| Keywords | Plain text, converted to tokens with `sherpa-onnx-cli text2token`; per-keyword boosting score and trigger threshold, no retraining |
| Runtime | Native library built with the NDK or taken from the `sherpa-onnx-vX-android.tar.bz2` release; about 7.2 MB per ABI, of which `libonnxruntime.so` is 5.8 MB |
| Packaging | No official Maven artifact; the AAR or the `.so` set must be built/vendored locally |

Integration constraints for this repository:

- The native runtime is larger than the 2 MB tracked-file limit enforced by
  `scripts/check_repo_hygiene.sh`. Do not commit the AAR: build it with a script into a gitignored
  directory (or publish it to a local Maven repository) and keep the build reproducible from a
  pinned sherpa-onnx release.
- The KWS engine must run inside a `microphone`-type foreground service. That means the wake-word
  service and the call service can be one service with a state machine (listening → in-call), which
  also avoids two notifications and two microphone clients competing for the input.
- Alternatives rejected: Picovoice Porcupine (proprietary, access key, fixed keyword set on the free
  tier), openWakeWord (TFLite, fixed models, Python-first tooling), Google Speech Recognizer
  (Play Services dependency and the only cloud-ish hop in the loop).

## Measured on device

Device: Google Chromecast with Google TV (`ro.product.device=sabrina`), Android 14 (API 34),
`ro.build.characteristics=tv,nosdcard`, output device HDMI only, **no** `android.hardware.microphone`
feature. Toolchain: AGP 8.11.0, Kotlin 2.3.21, `compileSdk` 36, `minSdk` 26, `targetSdk` 34.

Two build variants exist. The dependency-free one is 897 KB. The `probe` variant links the Pipecat
transport and measures 51.9 MB: 46.1 MB of that is `libjingle_peerconnection_so.so` for four ABIs
(11.6 MB arm64-v8a, 15.6 MB x86_64), which is the real budget for a release build that ships arm64
only (~12 MB). Linking it also pulls `androidx.appcompat` through `ai.pipecat:client`, so the client
module cannot stay `android.useAndroidX=false`.

### Screen-off session

Setup: stub relay on the host, app pointed at it over Wi-Fi with the endpoint passed at runtime
(`--es host ... --ei port ...`; nothing is hard-coded), generated 660 Hz upstream tone (the device has
no microphone), `USAGE_MEDIA` rendering, battery-optimisation exemption granted, display off.

Session length: 11 min 5 s of screen-off time, with the device forced into deep idle partway through.

| Observation | Result |
|---|---|
| Service state at the end | `isForeground=true`, `types=00000082` (`microphone|mediaPlayback`), process alive |
| Upstream | 33 226 frames in 665 s = 49.96 frames/s (20 ms PCM frames); relay measured RMS 4632–4659 against the expected 4634 for a 0.2 full-scale 660 Hz tone, i.e. byte-exact PCM end to end; 21.3 MB sent |
| Downstream | 33 224 frames in the same window = 49.96 frames/s, 21.3 MB received; jitter queue never overflowed (`dropped=0`) |
| Device state | `mWakefulness=Asleep`, `mState=IDLE` (forced deep idle) throughout |
| Whitelist effect | with the exemption present the stream continued through forced Doze; removing the whitelist mid-run did not stall the already-established socket (the exemption matters for wakeups and new connections, not for an active stream) |
| Rendering | audible on the TV with the display on (operator confirmation); with the display off the vendor HAL disconnects HDMI, reroutes to a phantom speaker and pauses the track, so playhead progress stops until the display returns (180 820 samples rendered across the whole session) |

The last row is a device artefact, not an app bug: the box has no speaker, and HDMI disappears when
the TV sleeps. On a phone the output device survives screen-off, which is the case the product cares
about. The demo's device callback and watchdog resume rendering as soon as an output returns.

### Audio path findings

- `AudioRecord` with `VOICE_COMMUNICATION`, 16 kHz mono, opened successfully; `AcousticEchoCanceler`
  and `NoiseSuppressor` are unavailable on this device; captured RMS stayed 0 (no microphone).
- `AudioTrack` with `USAGE_VOICE_COMMUNICATION` + `MODE_IN_COMMUNICATION`: `playState` never reached
  playing, `playbackHeadPosition` stuck at 640 samples, the track showed `Active: no` in
  `dumpsys media.audio_flinger`, 0 underruns — with the display on.
- `AudioTrack` with `USAGE_MEDIA` + `MODE_NORMAL`: real-time consumption (playhead advanced 75 093
  samples in 5 s, 0 underruns), audible.
- Consequence: the client must expose an output-mode switch rather than assuming communication audio
  works on every device class.

### SDK link check

A separate `probe` build type links `ai.pipecat:small-webrtc-transport:1.2.2` and resolves the SDK
reflectively at startup. On the device it logged:

```
sdk-probe: loaded ai.pipecat.client.small_webrtc_transport.SmallWebRTCTransport
sdk-probe: loaded org.webrtc.PeerConnectionFactory
sdk-probe: loaded native jingle_peerconnection_so
```

So the chosen transport compiles against this toolchain, its classes resolve, and the bundled
libwebrtc native library loads on Android 14/arm64. What is still untested is an actual RTVI call —
that belongs to the client scaffold task, not this spike.

### Bluetooth

No headset was paired on the test device, so only the negative path was exercised: the app enumerated
`availableCommunicationDevices` (speaker/HDMI only), logged "no headset", and left framework routing
in place. The `setCommunicationDevice()` code path is implemented in the demo but unverified against a
real headset — pair one and repeat before shipping.

## Open questions

1. Does the Pipecat transport's built-in audio path (communication mode) render correctly on the
   target phone? If not, the app must set mode/device itself before connecting.
2. Does the Pipecat transport route to a Bluetooth headset at all, or must the client own
   `setCommunicationDevice()`? Verify with a headset on the real client.
3. How much CPU and battery does continuous KWS cost at 160 ms chunks on the target phone? Measure
   with `dumpsys batterystats` over a one-hour listening session.
4. Does the relay's `/api/offer` path survive the phone's Wi-Fi power-save (screen off)? Test with a
   real RTVI client, not the TCP stub.

## Reproducing the spike

```bash
# stub relay (host side; loopback default, pass --host 0.0.0.0 for a Wi-Fi test)
python3 spike/stub-relay/relay.py --port 8765

# for a loopback-only test, tunnel the device to the host instead of hard-coding an address
adb reverse tcp:8765 tcp:8765

cd spike/android-demo
ANDROID_HOME="$HOME/Android/Sdk" ./gradlew assembleDebug    # dependency-free build
ANDROID_HOME="$HOME/Android/Sdk" ./gradlew assembleProbe     # links the Pipecat transport
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell pm grant com.example.hermestalk.spike android.permission.RECORD_AUDIO
adb shell am start -n com.example.hermestalk.spike/.MainActivity \
  --es host 127.0.0.1 --ei port 8765 --ez autostart true --ed tone_hz 660.0 --ez media_usage true
adb shell input keyevent 223     # display off
adb logcat -s HermesTalkSpike    # counters every 5 s

# always scope adb to the device under test, and stop the demo afterwards
adb -s <serial> shell am force-stop com.example.hermestalk.spike
```
