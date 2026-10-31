# spike — throwaway Android transport proof

This directory exists to answer one question on real hardware: **can a Hermes Talk client hold an
audio call with the screen off for ten minutes, on Android 14, without cloud dependencies?**

It is deliberately disposable. Delete `spike/` once `android/app/` and `android/rtvi/` carry the real
implementation; nothing outside `spike/` depends on it.

| Path | Purpose |
|---|---|
| `android-demo/` | Minimal Kotlin app: foreground microphone service, PCM capture/playback, relay client |
| `stub-relay/` | Dependency-free Python stub relay: sends a tone downstream, measures the upstream stream |

## What the demo proves

1. A foreground service declared with `foregroundServiceType="microphone|mediaPlayback"` keeps
   capturing and playing audio with the screen off and the device idle (measured, not assumed).
2. Audio round trip: the relay pushes a 440 Hz tone downstream (the app renders it with `AudioTrack`
   and logs the playback head position), the app pushes audio upstream (microphone capture, or a
   generated 660 Hz tone when no capture device exists) and the relay measures RMS per second.
3. Communication-device routing: the app enumerates `AudioDeviceInfo` and, when a Bluetooth headset
   is present, selects it with `AudioManager.setCommunicationDevice()`.

The transport is a plain length-prefixed TCP stream, not WebRTC. That is intentional: the spike
isolates the *app-side* concerns (service lifecycle, audio I/O, routing, battery state) so the
transport decision in `docs/research/android-stack.md` is not entangled with them. A `Transport`
interface marks where the RTVI/SmallWebRTC client or the LiveKit `Room` slots in.

A second build type, `probe`, links `ai.pipecat:small-webrtc-transport` so the AAR, its classes and
its libwebrtc native library are exercised on real hardware without changing the dependency-free
debug build:

```bash
ANDROID_HOME="$HOME/Android/Sdk" ./gradlew assembleProbe
# applicationIdSuffix .probe; logs "sdk-probe: loaded ..." on startup
```

Measured: 897 KB dependency-free debug APK, 51.9 MB probe APK (46.1 MB of libwebrtc `.so` for four
ABIs; 11.6 MB for arm64-v8a).

## Run it

Relay on the host (defaults to loopback; no LAN address is hard-coded anywhere):

```bash
python3 stub-relay/relay.py --port 8765
```

Make the device reach the host without knowing its IP, using an adb reverse tunnel:

```bash
adb reverse tcp:8765 tcp:8765
```

Build and install:

```bash
cd spike/android-demo
ANDROID_HOME="$HOME/Android/Sdk" ./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Start the call (or press *Start* in the app), then drive the soak from the host:

```bash
adb shell am start-foreground-service -n com.example.hermestalk.spike/.CallService \
  -a start --es host 127.0.0.1 --ei port 8765
adb shell input keyevent 26          # screen off
adb logcat -s HermesTalkSpike        # counters every 5 s
```

The relay prints one status line per second; a rising `up_frames` and `down_frames` count with the
screen off is the pass condition.

## Device notes

The spike was proven on a Google Chromecast with Google TV (Android 14, API 34, TV-only, no
microphone feature). Two hardware facts shaped the code and are documented in
`docs/research/android-stack.md`:

- The box has no capture device: `AudioRecord` opens but returns silence, so the upstream tone is
  generated. A phone must be used to verify the microphone path.
- The box has no speaker and its HDMI output disappears when the display sleeps, so the vendor audio
  HAL pauses playback tracks. The transport keeps flowing (frames are counted on arrival, a bounded
  queue drops rather than blocks) and rendering resumes when the display returns. On a phone this
  does not happen.

Run the demo only for bounded tests and force-stop it afterwards:

```bash
adb -s <serial> shell am force-stop com.example.hermestalk.spike
```

## Protocol

Both directions use the same framing: `uint32` big-endian payload length followed by PCM S16LE mono
16 kHz samples, 20 ms per frame (640 bytes). The stub relay speaks only this; it has no auth, no
encryption, and no reconnection semantics.
