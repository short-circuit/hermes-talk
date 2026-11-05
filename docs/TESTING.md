# Testing

## Layers

- **Unit** — parsers, event mapping, attachment validation, approval policy.
- **Integration** — relay against a stub Hermes API (recorded SSE fixtures); no real agent needed.
- **End-to-end** — relay + Hermes + local models, driven by scripted audio fixtures.
- **Android** — instrumentation tests for the call state machine; emulator for UI flows; one scripted-microphone call through `MainActivity` against the containerised relay (`scripts/e2e-emulator-container.sh`).
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

Two limits are worth recording. `/health` only proves liveness, and the probe only
proves that the emulator can reach the container's HTTP surface. The RTVI call
loop itself is exercised by the scripted-microphone test below, which opens a
real `/start` → offer → data-channel session on the same container. And the
emulator's host alias (`10.0.2.2`) reaches the host loopback at the socket level
but is unusable for this app over cleartext, because the network security config
permits cleartext only for loopback names; see `docs/DEPLOYMENT.md` ("Emulator
access").

## Scripted-microphone E2E call against the container relay

`com.example.hermestalk.e2e.ContainerCallE2ETest` drives the production path
through `MainActivity`: connect → `start_call` → scripted utterance →
endpointing → STT → brain → TTS audio, and asserts on the app's own event stream
(`CallTrace`, written by `PipecatEventBridge`, `RtviSession` and the audio
source). The relay is the real `hermes-talk-relay:local` container from
`deploy/compose.yaml`, reached over `adb reverse`; the WebRTC peer connection,
the RTVI signalling surface, VAD, STT, the brain router and TTS are the shipped
implementations. **No fixture or stub relay sits between the app and the relay**,
and no cloud STT/TTS/LLM is contacted.

### What is scripted, and why

Only the microphone samples. The emulator cannot be driven to speak reliably, so
`FixtureAudioSource` reads
`android/app/src/androidTest/assets/e2e/what_time_is_it.wav` — a synthetic
Kokoro utterance ("What time is it?") plus a 1.6 s silence tail, generated by
`scripts/make_e2e_utterance.py` (no personal recording, 90 KB) — and streams it
as 20 ms PCM16 16 kHz frames. Before the first frame the session sends an RTVI
client message declaring `audio-source {scripted: true}`; the relay then reads
those frames into the *same* `PcmChunk` queue that the WebRTC microphone track
feeds, and ignores the (silent) RTP track. The transport, the container and every
downstream stage stay real; only the samples are pre-recorded.

### Reproduce

```bash
scripts/e2e-emulator-container.sh
```

The script refuses to run without a booted emulator, starts the container stack
with `scripts/relay-container-up.sh` only when `/ready` does not already answer
`200` (a healthy relay is never recreated), sets up `adb reverse`, issues a
short-lived device token through `python -m relay.tokens issue`, runs exactly
that one instrumentation class, prints the app-observed latency line and the
JUnit XML summary, and revokes the token on exit.

### Result

Verified on 2026-11-05 on `hermes_talk_34` (API 34, x86_64, `emulator-5554`,
KVM) against `hermes-talk-relay:local` from `deploy/compose.yaml`, three
consecutive runs of the single command:

| Check | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| instrumentation | `tests="1" failures="0" errors="0" skipped="0"`, 9.4 s | same, 8.7 s | same, 8.1 s |
| app transcript event | `WHAT TIME IS IT` (final) | same | same |
| bot reply event | `It's 03:02.` | `It's 03:09.` | `It's 03:12.` |
| TTS audio reported by the relay and read by the app | 178 560 B / 1860 ms | 184 320 B / 1920 ms | 147 840 B / 1540 ms |
| container logs | `rtvi.audio_source_declared scripted=true`; `rtvi.audio_frame_accepted` total 91 734 B = the whole fixture | same | same |

Latency as measured from the app's event stream, anchored at the end of the
spoken words (not at the end of the file's silence tail):

| App-observed gap | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| speech end → final transcript (relay endpointing + local STT) | 1539 ms | 1541 ms | 1539 ms |
| final transcript → bot LLM text (brain) | 0 ms | 0 ms | 0 ms |
| bot LLM text → bot TTS started | 0 ms | 0 ms | 0 ms |
| bot TTS started → bot audio stats (local TTS synthesis) | 568 ms | 593 ms | 579 ms |
| speech end → bot audio stats (whole spoken answer ready) | 2107 ms | 2134 ms | 2118 ms |

The 0 ms gaps are real, not a rounding artefact: the utterance hit the
deterministic policy route for time questions, so the relay emitted the final
transcript, the `bot-llm-text` and `bot-tts-started` messages in one event-loop
tick before synthesising. The audio byte count varies with the reply text
(the clock reading changes between runs), so it is not a fixed fixture value.

### Failure semantics

The test **fails, it does not skip**, when the container is unreachable. Verified
by removing the `adb reverse` forward and re-running the class with a valid
token:

```
tests="1" failures="1" errors="0" skipped="0"
java.lang.AssertionError: the container relay must answer http://127.0.0.1:8790/ready
with status=ready; start it with scripts/relay-container-up.sh
(a stub relay on the same port does not count)
```

### Caveats

- The scripted path carries microphone audio as base64 JSON over the RTVI data
  channel instead of RTP. That adds serialisation and queueing, so the
  endpointing figure is an upper bound for a real device microphone; the RTP
  path is unchanged and is what the device uses.
- These are single app-observed samples, not a distribution. The endpointing+STT
  number exceeds the "STT final < 300 ms" target in *Latency SLOs* below: the
  relay's VAD hangover (`VadConfig.hangover_ms = 300`) is followed by local
  Whisper transcription of 1.2 s of speech on this host's CPU, which accounts for
  the remaining ~1.2 s. The test budgets are deliberately loose (15 s for
  endpointing+STT) so a loaded host does not produce a false failure; the SLO
  table records the target, the table above records what this hardware does.
- The reply came from the deterministic policy route. `scripts/e2e-emulator-container.sh`
  warns when `VOICE_BRAIN_BASE_URL` is not answering; in that case a non-policy
  utterance would speak the failure line instead of a model answer.
- The test is annotated `@LargeTest`, so the default instrumentation suite
  excludes it: it needs a booted emulator *and* the container stack. It runs in
  the dedicated `android-container-e2e` CI job (see *CI gates*), which downloads
  the pinned model set and starts the committed stack; locally the same path is
  `scripts/e2e-emulator-container.sh`.

## CI gates (GitHub Actions)

`ci.yml` runs two emulator jobs that exercise this behaviour; both are gates, not
smoke tests.

`android-instrumentation` runs `scripts/android-instrumentation.sh`, which:

1. starts the stub relay and `adb reverse` (the fixture the call tests expect —
   a real relay on 8790 rejects the empty bearer token a headless run passes, so
   the script refuses to guess and stops with a message if one answers);
2. installs the APKs and grants `android.app.role.ASSISTANT` through the shell
   (`cmd role add-role-holder`), then asserts the platform role-holder list names
   the package. A headless emulator cannot grant the role through the user
   dialog, so the shell grant is the only path;
3. runs the default suite (`notAnnotation=LargeTest`) with
   `screenOffSeconds=15` and `persistentSoakSeconds=15`, then verifies that
   `AssistantRoleHeldTest` (role held; `input keyevent 219` — keycode
   `KEYCODE_ASSIST` — resumes `MainActivity` on the call screen),
   `CallForegroundServiceTest#callSurvivesScreenOffAndReleasesTheMicrophoneWhenEnded`,
   `PersistentAssistantServiceTest#screenOffAndDozeKeepTheServiceAlive`,
   `AssistantWakeWordServiceTest#wakeWordDetectionLaunchesTheAssistantWithinBudget`
   and `#fixturesAreDetectedWithZeroMissesAndNoFalseTriggers` all ran and passed;
4. runs the one `@LargeTest` case the default suite excludes —
   `AssistantWakeWordServiceTest#continuousListeningForTheConfiguredWindow` — with
   `wakeWordSoakSeconds=120`, and verifies it the same way.

`scripts/verify-instrumentation-results.sh` reads the JUnit XML and fails when a
required case is absent, when any case failed or errored, or when anything was
skipped. A skip is not a pass: a filter that matches nothing, an APK that failed
to load, or an `@Ignore`d test would otherwise leave the job green.

`android-container-e2e` runs `scripts/e2e-emulator-container.sh` against the
committed stack. Model weights are gitignored, so `scripts/fetch-e2e-models.sh`
downloads the pinned public set and verifies every file by SHA-256 (the digests
are recorded in the script); the directory is cached between runs. Fetching is
not inference: STT, the deterministic brain route and TTS still run in the
container on the runner, so no cloud STT/TTS/LLM is contacted. The test is
`@LargeTest` because it needs the host model mount and the container, and it
fails rather than skips when the relay is unreachable.

Both jobs upload their instrumentation results as artifacts, so a red run keeps
the JUnit XML.

Run record: [run 42036584341](https://github.com/short-circuit/hermes-talk/actions/runs/42036584341)
on `feat/rtvi-call-loop` at `295d9ff` — all six jobs green.
`android-instrumentation` installed the APKs, granted the role
(`instrumentation: assistant role held by com.example.hermestalk`), ran 35 tests
(`0 failure(s), 0 error(s), 0 skipped`), verified all six required cases
(including `assistKeyOpensTheAssistantOnTheCallScreen`), then ran the 120 s
`@LargeTest` soak (1 test, 0 skipped). `android-container-e2e` downloaded and
verified the pinned weights, brought the committed stack up (`relay healthy;
/health and /ready answer 200 on 127.0.0.1:8790`), issued a device token and ran
`ContainerCallE2ETest` (1 test, 0 skipped) with a real app-observed line:
`endpointing+stt=1569ms brain=0ms tts_start=0ms tts_audio_report=2477ms
tts_audio=124800B/1300.0ms transcript='WHAT TIME IS IT' reply='It's 13:40.'`.

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

## Persistent assistant service: screen-off, doze, task swipe and reboot

The always-on assistant is a `START_STICKY`, `stopWithTask=false` foreground
service in `:app:call`. It is opt-in (plain SharedPreferences, default off) and
restarts from `BOOT_COMPLETED`/`MY_PACKAGE_REPLACED`. On Android 14+ the service
tries the microphone foreground type first and falls back to `specialUse` when a
background start is refused, recording the exception in
`PersistentAssistant.state.micRefusal` and promoting back to `microphone` on the
app's `ON_RESUME`.

### Test inventory

- JVM policy (`android/app/call/src/test/java/com/example/hermestalk/call/PersistentAssistantPolicyTest.kt`):
  attempt order with and without `RECORD_AUDIO` for SDK 26/29/33/34/35,
  deterministic microphone→specialUse fallback when the microphone attempt is
  refused, all-types-fail handling, wake-lock scoping, notification text
  selection, `START_STICKY`, refusal classification for the three exception
  types, and the default-off settings store round-trip.
- Instrumentation (`android/app/src/androidTest/java/com/example/hermestalk/persistent/PersistentAssistantServiceTest.kt`):
  `backgroundStartUsesThePlatformAllowedForegroundType`,
  `foregroundStartUsesMicrophoneTypeAndScopesTheWakeLock`,
  `screenOffAndDozeKeepTheServiceAlive` (default soak 600 s, overridable) and
  `persistentServiceIsDeclaredStopWithTaskFalse`.
  Instrumentation grants the target process background-activity-start
  privileges, so the emulator may accept `microphone` after Home; the real
  background refusal is exercised by the JVM fallback test instead.

### Commands

```sh
cd android
./gradlew :app:call:testDebugUnitTest :app:testDebugUnitTest --console=plain
./gradlew :app:assembleDebug --console=plain
```

```sh
adb shell am instrument -w \
  -e class com.example.hermestalk.persistent.PersistentAssistantServiceTest \
  -e persistentSoakSeconds 600 \
  com.example.hermestalk.test/androidx.test.runner.AndroidJUnitRunner
```

```sh
adb shell dumpsys deviceidle whitelist +com.example.hermestalk
adb shell dumpsys deviceidle whitelist | grep hermestalk
adb shell dumpsys deviceidle whitelist -com.example.hermestalk
```

### Results

Run on 2026-11-05 against the archive AVD `hermes_talk_34` (API 34, x86_64,
`emulator-5554`), from the working tree at `feat/rtvi-call-loop`.

JVM gates: `./gradlew :app:call:testDebugUnitTest :app:testDebugUnitTest` →
`BUILD SUCCESSFUL`, 519 tests across the modules with 0 failures and 0 errors
(`PersistentAssistantPolicyTest` 11/11). `./gradlew :app:assembleDebug` →
`BUILD SUCCESSFUL`.

Instrumentation: `scripts/android-instrumentation.sh` (the CI gate) with
`notAnnotation=androidx.test.filters.LargeTest`, `screenOffSeconds=15` and
`persistentSoakSeconds=15` → 35 tests, 0 failures, 0 errors, 0 skipped,
`BUILD SUCCESSFUL in 4m 05s`, including `AssistantRoleHeldTest` (role held;
`input keyevent 219` opens the call screen) and the wake-word service cases. The
script's verifier required all six named cases and found no skips; the separate
`@LargeTest` soak run with `wakeWordSoakSeconds=120` then passed with 1 test, 0
failures, 0 skipped. The merged manifest was re-read from
`app/build/intermediates/merged_manifests/debug/processDebugManifest/AndroidManifest.xml`.

| Test | Scenario | Outcome | Measured |
|---|---|---|---|
| `PersistentAssistantPolicyTest` | JVM policy: attempt order, deterministic fallback, wake lock, notification, sticky restart, refusals, settings store | passed | 11/11, 0 failures |
| `backgroundStartUsesThePlatformAllowedForegroundType` | Home press; asserts the platform-allowed microphone or specialUse type and the dumpsys foreground state | passed | 4.142 s; instrumentation kept `PROC_STATE_TOP`-like privileges, so it asserted the microphone type it was actually granted |
| `foregroundStartUsesMicrophoneTypeAndScopesTheWakeLock` | Visible start, microphone type, wake lock acquired/released with listening | passed | 1.084 s; wake lock acquired on `listening=true`, released on `listening=false` |
| `screenOffAndDozeKeepTheServiceAlive` | Screen off plus forced Doze for the soak duration, same pid and microphone type | passed | 601.098 s soak, same pid, `types=00000080` (microphone) in dumpsys |
| `persistentServiceIsDeclaredStopWithTaskFalse` | Merged manifest `stopWithTask=false` and declared foreground types | passed | `FLAG_STOP_WITH_TASK == 0`, microphone and specialUse declared |

### Manual acceptance: screen-off, task swipe, reboot

The soak test above covers the 600 s window, and the same scenario was repeated
by hand from the app UI (toggle on, then `input keyevent 26` and
`dumpsys deviceidle force-idle`) so the numbers below are independent of the
instrumentation process.

| Check | Command | Result |
|---|---|---|
| Service foreground with the microphone type | `dumpsys activity services com.example.hermestalk` | `isForeground=true foregroundId=4201 types=00000080` (microphone), `startCommandResult=1` (START_STICKY), `createdFromFg=true` |
| Screen off + forced Doze for 10 min | `input keyevent 26`; `dumpsys deviceidle force-idle` | `mState=IDLE`, same pid `21475` before and after 05:06:47Z → 05:16:47Z, service still foreground with microphone type |
| Wake lock scoping | `dumpsys power` while `listening=false` | `Wake Locks: size=0`; the instrumentation test asserts it is held only while listening |
| Task swipe | recents card swiped away | `topResumedActivity=NexusLauncherActivity`, zero `hermestalk` tasks left, service still foreground/microphone, pid unchanged |
| Reboot without a user launch | `adb reboot`; wait for `sys.boot_completed=1` | service restarted by `BOOT_COMPLETED` (`createdFromFg=false`, `allowStartForeground=BOOT_COMPLETED`, `uidState: RCVR`), `types=40000000` (specialUse), no activity resumed (`topResumedActivity=NexusLauncherActivity`) |
| Android 14 while-in-use refusal at boot (actual result) | logcat after the reboot | `W ActivityManager: Foreground service started from background can not have location/camera/microphone access: service com.example.hermestalk/.call.PersistentAssistantService` and `W PersistentAssistant: Microphone foreground type refused (java.lang.SecurityException: Starting FGS with type microphone … targetSDK=34 … and the app must be in the eligible state/exemptions to access the foreground only permission); falling back`. The platform raised `SecurityException`, not the `ForegroundServiceStartNotAllowedException` the Android 15 notes name; the service catches both and the boot start is recorded as `mAllowWhileInUsePermissionInFgsReason=DENIED` |
| Sticky restart | `run-as com.example.hermestalk kill -9 3548` | new pid `4145`, service foreground again within 3 s, `types=40000000` (specialUse, background restart), `startCommandResult=1` |
| Promotion once visible | launch `MainActivity` | `types=00000080` (microphone), `mAllowWhileInUsePermissionInFgsReason=PROC_STATE_TOP`, Settings shows `Running (microphone)` |
| Battery exemption UI | Settings card | the exemption button and hint disappear once `isIgnoringBatteryOptimizations` is true; the card otherwise offers **Allow background activity** |
| Doze whitelist command | `dumpsys deviceidle whitelist +com.example.hermestalk` | `Added: com.example.hermestalk`, listed as `user,com.example.hermestalk,10250` |

### Battery cost

Measured with `dumpsys battery unplug` and `dumpsys batterystats --reset`, then
10 minutes of screen-off forced deep idle with the service running and
`listening=false` (`2026-11-05T05:17:34Z` → `05:27:34Z`, 10m 04s of
batterystats time, 99.1% of it screen off):

- App-attributed power: uid `u0a250` does not appear in the estimated-power
  table at all, i.e. below the print threshold — 0.00 mAh for the window.
- Device deep-idle baseline (all global components): `screen 0.000225 +
  cpu 0.00358 + mobile_radio 0.000667 + sensors 0.0157 + wifi 0.0000218 +
  wakelock 0.0166 + idle 0.0168 + GPU 0.000667 = 0.0543 mAh`, i.e. about
  **0.32 mAh/h ≈ 0.011 %/h** against the AVD's learned 3000 mAh capacity.
- The emulator's simulated battery level is pinned at 100 while plugged in and
  does not step down when unplugged, so the level delta is unusable; the figure
  above is derived from `batterystats` estimated power, not from a level change.
- `dumpsys power` reported `Wake Locks: size=0` for the whole window, so the
  service adds no wake-lock cost when it is not listening.

## Always-on wake word inside the persistent assistant service

When the persistent assistant is enabled, `PersistentAssistantService` also runs
the on-device keyword spotter from `:wakeword` (`AssistantWakeWordRunner`). The
runner is owned by the service, not by `MainActivity`: it starts listening on
service start, restores the audio path after every detection, and launches the
assistant with an `ACTION_ASSIST` intent carrying
`EXTRA_WAKE_WORD = true`. Detection telemetry is logged under the
`AssistantWakeWord` tag, with the aggregate counters and launch budget under
`WakeWordUsage`. The Settings screen shows the enabled toggle, the current state
and the detection/false-trigger counts.

Microphone ownership is coordinated by `AssistantMicCoordinator`: a live call
always wins. Starting a call suspends the runner (the capture thread exits and
`AudioRecord` is released), and ending the call resumes it only when wake word
and the persistent assistant are still enabled. Disabling the wake word releases
the same resources. `AssistantWakeWordPolicy` is the single decision point for
these rules, including the 1000 ms detection-to-call budget and the restart
delay.

### Test inventory

- JVM policy (`android/app/call/src/test/java/com/example/hermestalk/call/AssistantWakeWordPolicyTest.kt`):
  enablement gates, microphone foreground type, `RECORD_AUDIO`, call precedence,
  restart delay, detection-step order and budget boundaries.
- JVM runner (`android/app/call/src/test/java/com/example/hermestalk/call/AssistantWakeWordRunnerTest.kt`):
  ordered detection steps, resume after the delay, suspend while a call is live,
  disable/release idempotence and the measured-latency budget flag.
- Instrumentation
  (`android/app/src/androidTest/java/com/example/hermestalk/persistent/AssistantWakeWordServiceTest.kt`):
  `fixturesAreDetectedWithZeroMissesAndNoFalseTriggers` (the whole committed
  fixture set through the service capture path),
  `wakeWordDetectionLaunchesTheAssistantWithinBudget`,
  `liveCallSuspendsWakeWordAndResumeRestoresCapture`,
  `disablingWakeWordStopsCaptureAndReleasesAudio` and
  `continuousListeningForTheConfiguredWindow` (`@LargeTest`, default soak
  1800 s, overridable with `-e wakeWordSoakSeconds <seconds>`; the CI gate runs
  it explicitly with 120 s after the default suite).
  The tests replace the capture with `FixtureWakeWordCapture`, which feeds the
  checked-in positive/negative WAV fixtures through the real keyword engine; the
  service, runner, policy and audio lifecycle stay real. The fixture-set test
  stubs only the Activity launch (the production launch starts a relay call,
  which correctly suspends the wake word); the launch path is covered by
  `wakeWordDetectionLaunchesTheAssistantWithinBudget`.

### Commands

```sh
cd android
./gradlew :app:call:testDebugUnitTest :wakeword:test :app:testDebugUnitTest --console=plain
./gradlew :app:assembleDebug --console=plain
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell am instrument -w \
  -e class com.example.hermestalk.persistent.AssistantWakeWordServiceTest \
  com.example.hermestalk.test/androidx.test.runner.AndroidJUnitRunner
```

### Results

Run on 2026-11-05 against the archive AVD `hermes_talk_34` (API 34, x86_64,
`emulator-5554`), from the working tree at `feat/rtvi-call-loop`.

JVM gates: `./gradlew :app:call:testDebugUnitTest :wakeword:test
:app:testDebugUnitTest` → `BUILD SUCCESSFUL`; the JUnit XML records
`:app:call` 82 tests, `:wakeword` 60 tests and `:app` 9 tests, all with
`failures="0" errors="0" skipped="0"` (151 tests total).
`./gradlew :app:assembleDebug` → `BUILD SUCCESSFUL`; `adb install -r` →
`Performing Streamed Install` / `Success`. The full `./gradlew test` suite over
every module (debug and release variants) reports 560 tests, `failures="0"`,
`errors="0"`, `skipped="0"`.

Instrumentation, full class with a shortened soak
(`-e wakeWordSoakSeconds 10`): `OK (5 tests)`, `Time: 185.09`, 0 failures and
0 errors. The soak test was also run once with its default 1800 s window before
the fixture-set test was added (production code unchanged since):
`OK (4 tests)`, `Time: 1,814.792`, 0 failures and 0 errors.

| Test | Scenario | Outcome | Measured |
|---|---|---|---|
| `fixturesAreDetectedWithZeroMissesAndNoFalseTriggers` | all 10 positive fixtures followed by the 3 negatives played through the service capture path | passed | `fixture_set positives=10 negatives=3 detections=10 false_trigger_count=69` — one detection per positive, no negative hit |
| `wakeWordDetectionLaunchesTheAssistantWithinBudget` | positive fixture detected while the service is listening; `ACTION_ASSIST` launch and `CallScreen` visibility | passed | `detection_launch_latency_ms=130 within_budget=true detection_count=1 false_trigger_count=35` |
| `liveCallSuspendsWakeWordAndResumeRestoresCapture` | call start releases `AudioRecord` and stops the capture thread; resume reacquires both; service stop releases them | passed | assertions held across suspend, settle and resume |
| `disablingWakeWordStopsCaptureAndReleasesAudio` | stored toggle off stops capture and releases audio; toggle on starts it again | passed | assertions held on disable and re-enable |
| `continuousListeningForTheConfiguredWindow` | screen-off soak with the same pid, service running and capture alive | passed | `listening_wall_ms=1800138 cpu_ms=74790 detection_count=0` over 1800 s |

Detection step timings from the `AssistantWakeWord` logcat lines emitted by the
positive-fixture test (all steps completed inside the 1000 ms budget):

| Step | Measured |
|---|---|
| `StopCapture` | 112 ms |
| `ReleaseListeningWakeLock` | 1 ms |
| `AbandonAudioFocus` | 1 ms |
| `ClearCommunicationDevice` | 1 ms |
| `ExitCommunicationMode` | 1 ms |
| `LaunchAssistant` | 5 ms |

The `false_trigger_count` values are the recorded tail of the on-device
false-trigger log (accepted detections accumulate across runs on the same
device), not a per-test count; the fixture-set run added exactly 10 entries and
the launch run 1. The soak deliberately used the negative fixture, so its
`detection_count=0` is the expected result. The soak's CPU figure is measured
with the scripted capture source, which adds a 100 ms sleep loop; the real
`AudioRecord` cost measured by `:wakeword`'s `WakeWordBatteryTest` is
83.9 s CPU per hour (2.3% of one core), i.e. roughly 11-17 mAh/h by the
documented energy model.

### Background-activity-launch limitation

`AssistantWakeWord` launches `MainActivity` from a foreground service. A
foreground service is **not** a background-activity-launch (BAL) exemption on
Android 14+, so the platform may silently refuse the `startActivity` while the
app is not visible. The launch path therefore records the measured latency and
any exception; the persistent notification's content intent remains the
one-tap fallback. Instrumentation grants the target process BAL privileges, so
the emulator accepts the launch and the tests can assert the `CallScreen`
appears. That privilege is not present on a stock device.

### Placeholder: real-device BAL verification

The emulator run proves detection, audio restore, call precedence and the
launch budget under granted BAL. It does **not** prove that a stock Android 14+
device will bring `MainActivity` to the foreground from the service. Record a
physical-device run here when one is available; until then this block is
intentionally unmeasured.

| Check | Expected | Result |
|---|---|---|
| screen off, wake word detected, no app visible | assistant opens within the 1000 ms budget, or the notification fallback is used | not run |
| launch refused by BAL | `lastLaunchStarted=false`, notification content intent opens the assistant | not run |
| call ends while wake word and assistant remain enabled | capture resumes and `AudioRecord` is reacquired | not run |
