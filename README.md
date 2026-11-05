# Hermes Talk

Local-only, hands-free voice control plane for [Hermes Agent](https://github.com/NousResearch/hermes-agent).
Talk to your agent from an Android phone, interrupt it mid-answer, approve or deny tool calls by voice,
and send files in either direction — with no cloud speech, LLM, or TTS dependency at runtime.

**Status:** v0.1.1, pre-alpha. Shipped: the relay's probe and file-transfer surface, the RTVI call loop
(WebRTC signalling and audio bridge, VAD/STT, voice brain, Hermes delegation, TTS, barge-in, spoken
approval and `MAX_CALL_MINUTES`), the Android client and its instrumentation suite. The Android
Connected/Ready path is still proven against `android/stub-relay`; an in-process `aiortc` client
exercises the production relay loop in the test suite. See `docs/RELEASE.md` for what v0.1.1 contains
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
needs tools, files, memory, or actions to Hermes, narrating progress while the agent works.

## Repository layout

| Path | Purpose |
|---|---|
| `relay/` | Python voice relay: WebRTC transport, speech pipeline, Hermes adapter |
| `android/` | Kotlin/Compose client |
| `docs/` | Architecture, ADRs, privacy model, testing, deployment, API reference, release checklist |
| `scripts/` | Repo hygiene checks, kanban seed script |

## Local-only invariant

No component requires internet at runtime. One-time model downloads are allowed; after that the system
must function on an isolated LAN. See `docs/PRIVACY.md` and `docs/adr/0001-local-only.md`.

## Quick start

Requires Python 3.11+, Docker, JDK 17, and Android SDK.

```bash
cp .env.example deploy/.env     # fill in endpoints and a device token
docker compose -f deploy/compose.yaml up -d
cd android && ./gradlew assembleDebug
```

## Default assistant

The Android app registers an `ACTION_ASSIST` entry point and can be granted the
system assistant role, so a long press of the power button opens the call screen.
Grant and verify the role holder from a development machine:

```sh
adb shell cmd role add-role-holder android.app.role.ASSISTANT com.example.hermestalk
adb shell cmd role get-role-holders android.app.role.ASSISTANT
```

## License

MIT — see `LICENSE`.
