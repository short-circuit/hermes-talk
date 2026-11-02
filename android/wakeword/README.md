# On-device wake word

This module adds local keyword spotting for the phrase **HEY HERMES** to Hermes
Talk. Audio is processed on the device by sherpa-onnx; no audio, transcript or
cloud request leaves the phone for wake-word detection. Push-to-talk is the
fallback when the keyword engine or its native library is unavailable.

## Model provenance

The bundled model is the English gigaspeech keyword-spotting model from
sherpa-onnx v2.0.1:

- model: `sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01`
- upstream: <https://github.com/k2-fsa/sherpa-onnx/releases/tag/v2.0.1>
- runtime assets, committed under `android/app/src/main/assets/wakeword/`:
  - `encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx` — 4,807,159 bytes
  - `decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx` — 277,985 bytes
  - `joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx` — 163,380 bytes
  - `tokens.txt` — 5,006 bytes
  - `keywords.txt` — 20 bytes (`▁HE Y ▁HER ME S`)

The Kotlin API under `src/main/java/com/k2fsa/sherpa/onnx/` is vendored from
sherpa-onnx v2.0.1 and kept verbatim. See [`THIRD_PARTY.md`](THIRD_PARTY.md).

## Native libraries are downloaded at build time

The Android JNI libraries are not committed. The sherpa-onnx v2.0.1 Android
archive is downloaded only when Gradle executes the `fetchSherpaOnnxJniLibs`
task, which `preBuild` depends on. There is no network access at configuration
time.

- archive: `sherpa-onnx-v2.0.1-android.tar.bz2`
- URL: <https://github.com/k2-fsa/sherpa-onnx/releases/download/v2.0.1/sherpa-onnx-v2.0.1-android.tar.bz2>
- SHA-256: `39003753ec293425ed56723ea9b972b98841a490af3311670c3060548d654330`

The task verifies the checksum, extracts only `jniLibs/arm64-v8a/` and
`jniLibs/x86_64/` into `layout.buildDirectory/jniLibs/sherpa-onnx/`, and skips
the download when that directory is already complete. A checksum mismatch
fails the build with a clear message and discards the download.

The model ONNX files are committed because the app needs them at runtime. They
are larger than the repository's default 2 MiB hygiene cap, so
`scripts/check_repo_hygiene.sh` documents a path-scoped R5 exception: files under
`android/app/src/main/assets/wakeword/*.onnx` may be up to 6 MiB. Every other
path keeps the 2 MiB cap. The native JNI blobs stay out of git because they are
large per-ABI binaries that change with the upstream release.

## Tuning and sensitivity presets

The defaults were measured on the host with the same model and fixtures:

| Setting | Value |
|---|---|
| `keywords_threshold` | `0.15` (Balanced) |
| `keywords_score` | `1.0` |
| `num_trailing_blanks` | `1` |
| `max_active_paths` | `4` |
| `num_threads` | `1` |
| `sample_rate` | `16000` |
| `feature_dim` | `80` |
| detection cooldown | `2500 ms` |
| capture chunk | `1600` samples (100 ms) |
| false-trigger log cap | `65536` bytes |

The sensitivity selector maps to the keyword threshold:

| Preset | Threshold |
|---|---|
| Sensitive | `0.10` |
| Balanced | `0.15` (default) |
| Strict | `0.25` |

The capture loop uses `MediaRecorder.AudioSource.VOICE_RECOGNITION`, 16 kHz mono
PCM16, and a buffer of at least twice `AudioRecord.getMinBufferSize`.

## Changing the phrase

The keyword file is one BPE token line. To change the phrase, regenerate the
line with the model's BPE vocabulary (from `tokens.txt`) and write exactly one
non-empty line to `android/app/src/main/assets/wakeword/keywords.txt`. A
trailing `@label` is optional; when present it must name the same phrase. The
module validates the line at parse time with `WakeWordKeywordFile`:

- exactly one non-empty line;
- at least one token;
- no duplicate lines;
- the token-joined form must match `WakeWordConfig.phrase`;
- a trailing `@label` must match the phrase.

For example, the bundled line tokenizes to `HEYHERMES`, which matches the
configured phrase `HEY HERMES` after removing the word-boundary marker.

## False-trigger log

Accepted detections are appended to a JSON-lines file in the app's private
files directory:

```text
<app files dir>/wakeword/events.log
```

Each line contains only numeric and keyword fields:

```json
{"ts":1730000000000,"keyword":"HEY HERMES","source":"wake_word","threshold":0.15,"listening_ms":1234}
```

`source` is `wake_word` or `push_to_talk`. The log is capped at the configured
byte budget (default 64 KiB). On overflow the current file becomes
`events.log.1` (one rotated backup) and a new `events.log` starts. Reads tolerate
a partially written last line. No absolute paths or personal data are written.
The log is local-only and is never uploaded.

## Push-to-talk fallback

The "Hold to talk" control drives a separate pure state machine
(`Idle`/`Pressed`/`Released`). Releasing the button emits a session request and
starts the existing call path. It does not depend on the keyword engine, so it
still works when the engine or native library is unavailable. Pressing the
button does not create an `AudioRecord`; the call path owns microphone capture.

## Foreground-only limitation

Wake-word listening runs while the app process is in the foreground. The module
does not create or restart a foreground service, and it stops listening when a
call starts so the call owns the microphone. Listening resumes when the call
ends and the wake word is still enabled. There is no background or screen-off
wake-word detection.

## Measured

Measured on the host with the bundled model and the same fixture set at the
Balanced preset:

| Check | Result |
|---|---|
| 33-utterance synthesized set (clean, 1 m quiet-room, low-gain) | 33/33 detected |
| 10 on-device fixture positives | 10/10 detected |
| 3 on-device fixture negatives | 0/3 false hits |
| false triggers per hour | pending |
| battery drain per hour | pending |

The instrumented runs fill in the pending rows. The battery test writes
`usage.json` to the app files directory and logs the same numbers under the
`WakeWordUsage` logcat tag.

## Instrumentation

The device tests need a connected emulator or device with the model assets in
the app APK:

```sh
cd android
./gradlew :app:connectedDebugAndroidTest
```

To run only the wake-word tests:

```sh
adb shell am instrument -w \
  -e class com.example.hermestalk.wakeword.WakeWordEngineTest \
  com.example.hermestalk.test/androidx.test.runner.AndroidJUnitRunner
```

`WakeWordBatteryTest` is annotated `@LargeTest` and defaults to 60 minutes of
continuous listening. Override the window when needed:

```sh
adb shell am instrument -w \
  -e class com.example.hermestalk.wakeword.WakeWordBatteryTest \
  -e wakeWordBatteryMinutes 60 \
  com.example.hermestalk.test/androidx.test.runner.AndroidJUnitRunner
```

Read the telemetry back with:

```sh
adb logcat -s WakeWordUsage
adb shell run-as com.example.hermestalk cat files/usage.json
```

## Unit tests

The pure logic is covered on the JVM:

```sh
cd android
./gradlew :wakeword:test
```

The suite covers configuration validation and preset mapping, keyword-file
parsing, cooldown gating, every legal and illegal lifecycle transition,
false-trigger log rotation and truncated-line recovery, push-to-talk
transitions, usage arithmetic and the settings store fake.
