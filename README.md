# Hermes Talk

Local-only, hands-free voice control plane for [Hermes Agent](https://github.com/NousResearch/hermes-agent).
Talk to your agent from an Android phone, interrupt it mid-answer, approve or deny tool calls by voice,
and send files in either direction — with no cloud speech, LLM, or TTS dependency at runtime.

**Status:** v0.1.1, pre-alpha. Shipped: the relay's probe and file-transfer surface, the RTVI call loop
(WebRTC signalling and audio bridge, VAD/STT, voice brain, Hermes delegation, TTS, barge-in, spoken
approval and `MAX_CALL_MINUTES`), the Android client and its instrumentation suite. The Android client
also keeps an always-on assistant foreground service alive across screen-off, task swipe and reboot when
the user opts in. The production call path is proven end to end against the containerised relay by
`ContainerCallE2ETest`; `android/stub-relay` remains the offline fixture for the instrumentation suite.
See `docs/RELEASE.md` for what v0.1.1 contains
and `docs/ROADMAP.md` for the phases.

## Why

Cloud voice stacks (OpenAI Realtime, Gemini Live) are excellent but bill per minute and ship your audio
off-site. Hermes Talk keeps the entire loop on hardware you own: speech recognition, the conversational
model, speech synthesis, the agent, and the tools all run on the LAN.

## Architecture

```
Android app (Kotlin/Compose)
   |  WebRTC, direct P2P on LAN
   v
voice-relay  (Python, Pipecat)
   |-- VAD / turn detection
   |-- STT (local)
   |-- voice brain (local LFM model, 4 tools)
   |-- TTS (local)
   |-- attachment inbox / artifact outbox
   v
Hermes API server (127.0.0.1)
   v
Hermes agent + tools (local inference host)
```

The voice brain never executes work itself. It answers small talk directly and delegates anything that
needs tools, files, memory, or actions to Hermes, narrating progress while the agent works. The
component map and the persistent-assistant service lifecycle are in `docs/ARCHITECTURE.md`.

## Repository layout

| Path | Purpose |
|---|---|
| `relay/` | Python voice relay: WebRTC transport, speech pipeline, Hermes adapter |
| `android/` | Kotlin/Compose client |
| `docs/` | Architecture, ADRs, privacy model, testing, deployment, API reference, release checklist |
| `scripts/` | Repo hygiene checks, emulator control, container E2E runner, kanban seed script |

## Local-only invariant

No component requires internet at runtime. One-time model downloads are allowed; after that the system
must function on an isolated LAN. See `docs/PRIVACY.md` and `docs/adr/0001-local-only.md`.

## Quick start

Requires Python 3.11+, Docker with compose, JDK 21, and an Android SDK with platform-tools plus an API 34
x86_64 system image.

**1. Start the relay.** Copy the example environment, fill in the Hermes API URL and key, the voice-brain
URL and model, the model paths and `RELAY_SITE_ADDRESS`, then bring up the committed stack:

```sh
cp .env.example deploy/.env
$EDITOR deploy/.env
scripts/relay-container-up.sh --with-proxy
curl -fsS http://127.0.0.1:8790/ready
```

`/ready` must return 200 with every check `ok` before the phone can place a call. `DEPLOYMENT.md` lists
the settings and the model paths. The script rebuilds and recreates the relay containers, so on an
already-running stack it is a restart — run it in a maintenance window or when the image changed.

**2. Issue a device token.**

```sh
cd deploy
docker compose -f compose.yaml exec -T relay python -m relay.tokens issue --label phone
```

The token is the bare `ht_…` line in the output. Keep it out of the repository and shell history.

**3. Build and install the app.**

```sh
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Android/Sdk}"
cd android && ./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell pm grant com.example.hermestalk android.permission.RECORD_AUDIO
adb shell pm grant com.example.hermestalk android.permission.POST_NOTIFICATIONS
```

**4. Run it on an emulator.** The AVD is kept outside the repository; set `ANDROID_AVD_HOME` to a
directory with room for a 6 GiB data partition (the script falls back to the reference host's archive
mount when the variable is unset):

```sh
export ANDROID_AVD_HOME=/path/to/avd-store
scripts/emulator-archive.sh headless
scripts/emulator-archive.sh wait-for-boot
adb reverse tcp:8790 tcp:8790
```

`adb reverse` is how the emulator reaches the loopback-only relay. On a physical phone, use the LAN
address of the proxy instead.

**5. Make it the assistant and keep it alive.** Grant the assistant role and the battery exemption, then
enable the persistent assistant in the app (Settings → **Background assistant** → **Keep assistant
running**):

```sh
adb shell cmd role add-role-holder android.app.role.ASSISTANT com.example.hermestalk
adb shell dumpsys deviceidle whitelist +com.example.hermestalk
```

Open the app, enter the relay URL and the token, and place a call. `docs/RUNBOOK.md` sections 12–13
cover the role, the exemption, boot start and `dumpsys` troubleshooting; `docs/TESTING.md` covers the
emulator procedure, the container E2E command and the measured baseline.

## Default assistant

The Android app registers an `ACTION_ASSIST` entry point and can be granted the
system assistant role, so a long press of the power button opens the call screen.
Grant and verify the role holder from a development machine:

```sh
adb shell cmd role add-role-holder android.app.role.ASSISTANT com.example.hermestalk
adb shell cmd role get-role-holders android.app.role.ASSISTANT
adb shell input keyevent 219   # KEYCODE_ASSIST
```

With the persistent assistant enabled, the same foreground service also runs
the on-device "Hey Hermes" keyword spotter: a detection launches the assistant
through `ACTION_ASSIST`, and a live call always takes the microphone first. The
wake-word toggle, counts and status live on the Settings screen. The service is
opt-in, survives screen-off and task swipe, and comes back after a reboot — with
the `specialUse` foreground type until the app is opened, because Android 14
refuses a microphone foreground service started from `BOOT_COMPLETED`. See
`android/README.md` ("On-device wake word", "Always-on assistant service") and
`docs/RUNBOOK.md` section 12 for the commands and expected `dumpsys` output.

## Testing

```sh
python -m pytest relay/tests          # relay suite
cd android && ./gradlew test          # JVM unit tests, no device
scripts/android-instrumentation.sh    # emulator suite (needs a booted AVD)
scripts/e2e-emulator-container.sh     # production call path against the container
```

`scripts/e2e-emulator-container.sh` is the fastest proof that the whole loop works: it issues a
short-lived token, runs `ContainerCallE2ETest` through real WebRTC, STT, voice brain and TTS, and fails
rather than skips when the container is absent. The CI gates and the measured baseline are in
`docs/TESTING.md`.

## License

MIT — see `LICENSE`.
