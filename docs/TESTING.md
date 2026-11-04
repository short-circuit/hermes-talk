# Testing

## Layers

- **Unit** — parsers, event mapping, attachment validation, approval policy.
- **Integration** — relay against a stub Hermes API (recorded SSE fixtures); no real agent needed.
- **End-to-end** — relay + Hermes + local models, driven by scripted audio fixtures.
- **Android** — instrumentation tests for the call state machine; emulator for UI flows.
- **Soak** — 30-minute call with induced network loss and reconnection.

## Emulator (archive-hosted AVD)

The Android layers run on an API 34 x86_64 AVD whose storage lives outside the
home partition:

| Part | Location |
|---|---|
| emulator binary, platform-tools, system image | `$ANDROID_SDK_ROOT` (default `$HOME/Android/Sdk`) |
| AVD `hermes_talk_34` (config, 6 GiB data partition, snapshots) | `$ANDROID_AVD_HOME` (default `/store/archive/android-avd`) |

The split is deliberate: the home partition runs at ~94% capacity while the
archive volume has over a terabyte free, and the userdata image grows with
installed builds, snapshots and app data — exactly what should not land on the
small disk. The system image is read-only and stays with the SDK so that
`avdmanager` and `emulator` can resolve `image.sysdir.1`.

`scripts/emulator-archive.sh` is the supported entry point. Every subcommand
exports the same `ANDROID_AVD_HOME` before invoking `avdmanager` or `emulator`,
so the two tools cannot disagree about where the AVD lives.

```bash
scripts/emulator-archive.sh headless        # boot without a window
scripts/emulator-archive.sh wait-for-boot   # block until sys.boot_completed=1
scripts/emulator-archive.sh stop            # shut the emulator down
scripts/emulator-archive.sh start           # same as headless, with a window
```

The script is idempotent: it reuses an existing AVD and refuses to start a
second emulator while one is attached, so repeated runs never fork a duplicate
AVD. `COLD_BOOT=yes` skips the quickboot snapshot when a reproducible boot is
needed. `ANDROID_SDK_ROOT`, `ANDROID_AVD_HOME`, `AVD_NAME`, `SYSTEM_IMAGE`,
`DEVICE_PROFILE`, `DATA_PARTITION_SIZE` and `BOOT_TIMEOUT` are overridable, so
no personal path is hard-coded in the repository.

Hardware acceleration is required. `emulator -accel-check` reports
`KVM (version 12) is installed and usable` on a host where `/dev/kvm` is
readable by the build user; without it the x86_64 image falls back to
emulation and boots several times slower.

Measured cold boot on this host (API 34 x86_64, 4 vCPU, KVM):

| Case | Time to `sys.boot_completed=1` |
|---|---|
| first boot of a freshly created AVD | 20.1 s (emulator log: `Boot completed in 20146 ms`) |
| cold boot with existing userdata (`COLD_BOOT=yes`) | 14.8 s (emulator log: `Boot completed in 14807 ms`), 16.1 s from process launch |

Verification after boot:

```bash
adb devices                                   # emulator-5554	device
adb shell getprop ro.build.version.sdk        # 34
```

## Baseline: the current app on the archive emulator

Recorded 2026-11-04 on `hermes_talk_34` (API 34, x86_64, `emulator-5554`, KVM) as
the pre-P7 reference point: what the app does today, before any P7 feature lands.
Every figure below is transcribed from that run's own output. The raw capture is
kept outside the tree (it is not committed, the repository is public) at:

```text
$XDG_CACHE_HOME/hermes-talk/baseline-2026-11-04/    (default: ~/.cache/hermes-talk/baseline-2026-11-04/)
```

| File | Contents |
|---|---|
| `assembleDebug-clean.log` | `./gradlew clean :app:assembleDebug`, 120 tasks, `BUILD SUCCESSFUL` |
| `install.log` | `adb install -r app-debug.apk`, `Performing Streamed Install` / `Success` |
| `permissions.log` | the three `pm grant` calls and the resulting `granted=true` states |
| `instrumentation.log` | stub relay log plus the full gradle output of the suite |
| `logcat.txt` | `adb logcat -d -v threadtime` for the test run (6552 lines) |
| `logcat-crash.txt` | crash buffer; empty, no crash during the run |
| `junit-results.xml` | the connected-test JUnit report (`tests="20" failures="0" errors="0" skipped="0"`) |
| `device.txt` | `adb devices`, SDK level, release, ABI, screen size |
| `run-baseline.sh` | the driver that produced all of the above |

### Toolchain and prerequisites

- JDK 21, Gradle 8.14.3 (wrapper), AGP 8.11.0, Kotlin 2.3.21, `compileSdk` 36,
  `targetSdk` 34, `applicationId com.example.hermestalk`, versionName 0.1.1.
- Gradle needs the SDK location: without `ANDROID_HOME`/`ANDROID_SDK_ROOT` (or
  `android/local.properties`) `:app:assembleDebug` fails with `SDK location not
  found`. Exporting `ANDROID_HOME` for the shell is enough; no file is written
  into the tree.
- `scripts/android-instrumentation.sh` defaults `PYTHON` to `relay-venv/bin/python`,
  which only CI creates. Locally `PYTHON=$PWD/.venv/bin/python` works: that
  environment already carries `aiortc` 1.15.0 and `av` 17.1.0, which is all the
  stub relay imports.

### Reproduce

```bash
export ANDROID_HOME=$HOME/Android/Sdk ANDROID_SDK_ROOT=$HOME/Android/Sdk
cd android && ./gradlew clean :app:assembleDebug --console=plain
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell pm grant com.example.hermestalk android.permission.RECORD_AUDIO
adb shell pm grant com.example.hermestalk android.permission.POST_NOTIFICATIONS
adb shell pm grant com.example.hermestalk android.permission.BLUETOOTH_CONNECT
adb logcat -c
PYTHON=$PWD/.venv/bin/python ../scripts/android-instrumentation.sh
adb logcat -d -v threadtime > "$XDG_CACHE_HOME/hermes-talk/baseline-2026-11-04/logcat.txt"
```

### Build and install

`./gradlew clean :app:assembleDebug` → `BUILD SUCCESSFUL in 7s`, 120 actionable
tasks (59 executed, 61 from cache). APK `android/app/build/outputs/apk/debug/app-debug.apk`,
124,184,131 bytes. `adb install -r` → `Success`. Runtime permissions granted
before the run: `RECORD_AUDIO`, `POST_NOTIFICATIONS`, `BLUETOOTH_CONNECT`
(`granted=true` in `dumpsys package`).

### Instrumentation outcomes

Runner: `AndroidJUnitRunner`, `hermes_talk_34(AVD) - 14`. Gradle reported
`Starting 20 tests` … `Finished 20 tests` and `BUILD SUCCESSFUL in 45s`; the JUnit
XML records `tests="20" failures="0" errors="0" skipped="0"` with a suite time of
33.259 s. Logcat shows a `TestRunner: started:` and `finished:` pair for each of
the 20, and no `FATAL EXCEPTION` or `E/AndroidRuntime` line.

| Class | Test | Outcome | Time (s) |
|---|---|---|---|
| attachments.TokenExpiryTest | artifactListWithExpiredTokenIsReportedNotThrown | passed | 0.004 |
| attachments.TokenExpiryTest | uploadWithExpiredTokenIsTerminal | passed | 0.000 |
| attachments.TokenExpiryTest | sessionStartWithExpiredTokenFailsWithoutRetrying | passed | 0.022 |
| attachments.TokenExpiryTest | expiredTokenIsRejectedByTheLivenessProbe | passed | 0.001 |
| attachments.UploadRetryTest | queuedUploadResumesAfterProcessRestart | passed | 0.052 |
| attachments.UploadRetryTest | transientRelayFailuresAreRetriedAndTheUploadCompletes | passed | 0.051 |
| attachments.UploadRetryTest | unauthorizedUploadIsTerminalAndKeepsThePayloadForManualRetry | passed | 0.081 |
| call.CallForegroundServiceTest | callSurvivesScreenOffAndReleasesTheMicrophoneWhenEnded | passed | 18.559 |
| call.CallPermissionTest | deniedPermissionFailsCleanlyAndStopsTheService | passed | 0.855 |
| call.CallScreenTest | permissionDeniedStateShowsPermissionCardWithoutCrashing | passed | 0.632 |
| call.CallScreenTest | endButtonInvokesCallback | passed | 0.755 |
| call.CallScreenTest | activeCallShowsMuteButtonAndInvokesCallback | passed | 0.833 |
| call.CallScreenTest | batteryPromptIsShownWhenOptimisationIsNeeded | passed | 0.706 |
| call.CallStateTest | unreachableRelayFailsTheCallAndReleasesEverything | passed | 0.904 |
| call.CallStateTest | controlActionsAreIgnoredWhileNoCallIsLive | passed | 0.026 |
| e2e.StubRelayFlowTest | expiredTokenShowsTheUnauthorizedReasonWithoutCrashing | passed | 2.911 |
| e2e.StubRelayFlowTest | connectsToTheStubRelayAndCompletesACall | passed | 5.544 |
| wakeword.PushToTalkTest | pressReleaseRequestsASessionAndLeavesNoCaptureRunning | passed | 0.000 |
| wakeword.WakeWordEngineTest | fixturesDetectAtLeastNinetyPercentAndRejectNegatives | passed | 0.906 |
| wakeword.WakeWordPermissionTest | deniedPermissionReachesPermissionDeniedWithoutAudioRecord | passed | 0.000 |

### What is *not* covered, and why

Nothing was skipped in this run, and no test is counted as passed on the
strength of a skip:

- `wakeword.WakeWordBatteryTest` is annotated `@LargeTest` and the suite runs
  with `notAnnotation=androidx.test.filters.LargeTest`. It is excluded from the
  20, not passed: it is a 30-minute battery soak that does not fit the
  CI-budgeted run.
- `e2e.StubRelayFlowTest.connectsToTheStubRelayAndCompletesACall` is guarded by
  `Assume.assumeTrue("the stub relay must answer on 127.0.0.1:8790 …")`. It ran
  and passed here because the stub relay answered `/health` through
  `adb reverse tcp:8790 tcp:8790`. On a bare emulator without the fixture the
  runner records it as *skipped* with that message — the XML `skipped` count, not
  a green testcase, is what to trust.
- The production relay has no RTVI/WebRTC surface: `relay/app.py` serves `/`,
  `/health`, `/ready` and `/v1/*` but no `/sessions/*`, so `Connected`/`Ready`
  can only be proven against `android/stub-relay`. The app's liveness probe is
  the only part exercised against the real relay shape.

### Stub-relay call flow (real output)

`instrumentation.log` and `logcat.txt` show the fixture and the app agreeing end
to end. Timestamps are from the run, session ids are the relay's:

1. `21:39:31.646` `SmallWebRTCTransport: connect(.../sessions/02c656a0-…/api/offer, Authorization=Bearer stub-token)` → `Connected` `32.627`, `Ready` `32.628`; relay logged `POST /start` 200, `offer answered`, `bot-ready sent`, `client-ready received`.
2. `21:39:32.920` `Disconnected` → new session `816c0df0-…`: `Connected` `33.851`, `Ready` `33.852`; the relay logged `received audio track` + `received video track` for both sessions and counted 500 audio frames on the `fd5e307b-…` call before it was replaced.
3. Mute/unmute round-trip visible server-side as RTVI signalling on the `chat`
   channel: `trackStatus receiver_index=0 enabled=true`, `…enabled=false`,
   `…enabled=true` (`21:39:33.921`–`34.146`).
4. `21:39:34.305` `Disconnected`, relay logged `datachannel closed` / `audio track ended`.

So the call state machine, the RTVI handshake and the control messages work
against the stub contract today.

### Observations for P7 (baseline facts, not failures)

- The client never issues `DELETE /sessions/{id}`: no `DELETE` line appears in
  `instrumentation.log`, and the app sources contain no `delete(` call. Teardown
  relies on closing the peer connection, so the relay keeps its session entry
  until it is told otherwise.
- `CallPermissionTest` reaches `Ready` before the denial is injected
  (`21:39:23.483`), then fails the call; that transport stayed `Ready` and the
  relay kept counting audio frames until the next test replaced the singleton
  transport at `21:39:32.920` (`setState(Disconnected)`).
- Every trickle candidate the client PATCHes is rejected by the fixture with
  `candidate ignored: Candidate must have either sdpMid or sdpMLineIndex`
  (12 + 4 candidates per session). The handshake still completes because the
  fixture gathers ICE before answering, so loopback does not depend on trickle —
  but the PATCH path is effectively a no-op today and would matter as soon as
  the relay runs behind NAT.

## Container relay on the emulator

The baseline above runs against `android/stub-relay`. The production path is the
committed container stack, and it is reachable from the emulator through a
forwarded port:

```bash
scripts/relay-container-up.sh        # compose up --build, wait for healthy, adb reverse
# ... run the app or the probe test ...
scripts/relay-container-down.sh      # stop the stack, drop the forward
```

`ContainerRelayProbeTest` is the acceptance test: it drives the app's own
`HttpHealthProbe`/`RelayEndpoint` against `http://127.0.0.1:8790` and reads
`/ready` from inside the emulator. It skips with a clear message when no relay
answers, so it does not fail on a bare emulator or in CI.

```bash
cd android && ./gradlew :app:connectedDebugAndroidTest \
  -Pandroid.testInstrumentationRunnerArguments.class=com.example.hermestalk.e2e.ContainerRelayProbeTest
```

Verified on 2026-11-04 on `hermes_talk_34` (API 34, x86_64, `emulator-5554`,
KVM) with `hermes-talk-relay:local` from `deploy/compose.yaml`:

| Check | Result |
|---|---|
| `docker compose ps` relay | `Up (healthy)`; healthcheck probes `/health` and `/ready` |
| host `/health`, `/ready` | `200` with `{"status":"ready"}` and every check `ok` |
| `adb reverse tcp:8790 tcp:8790` | in place; the app probe reached the container |
| `ContainerRelayProbeTest` | `tests="2" failures="0" errors="0" skipped="0"` (0.073 s, 0.078 s) |
| proxy on `voice.example.lan:8443` | `Up (healthy)`; `/health` and `/ready` return `200` over TLS with the internal CA |

Two limits are worth recording. `/health` only proves liveness: the production
relay exposes `/health`, `/ready` and `/v1/*` but no RTVI `/start`, so the
Connected/Ready handshake is still only proven against the stub fixture. And the
emulator's host alias (`10.0.2.2`) reaches the host loopback at the socket level
but is unusable for this app over cleartext, because the network security config
permits cleartext only for loopback names; see `docs/DEPLOYMENT.md` ("Emulator
access").

## Fixtures


Synthetic only. Use short generated tones or a TTS-generated corpus; never commit personal recordings.
Keep fixtures under 2 MB.

## Latency SLOs

| Metric | Target |
|---|---|
| Endpointing | 300–500 ms |
| STT final | < 300 ms |
| Brain first token | < 300 ms |
| TTS first audio | < 250 ms |
| Perceived response | < 1.2 s p50 |

Failures are reported with the measured distribution, not a single sample.
