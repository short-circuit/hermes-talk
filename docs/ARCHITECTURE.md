# Architecture

**Scope.** This document describes the code as shipped at commit `3c1dcf7` (relay `0.1.0`,
pre-alpha). Every diagram names real modules and symbols; if a diagram disagrees with the tree, the
diagram is wrong — fix it in the same change that moves the code.

**Audience.** A contributor who needs to locate a component, know what crosses which boundary, and
extend the relay without breaking the Hermes contract.

Related documents: `docs/adr/0001-local-only.md`, `docs/adr/0002-transport.md`,
`docs/research/hermes-api.md` (observed gateway behaviour, section numbers cited below),
`docs/security/threat-model.md` (T-ids), `docs/DEPLOYMENT.md` (ports, health gate, rollback),
`docs/RUNBOOK.md` (operations), `docs/PRIVACY.md` (data flow and repository hygiene rules),
`docs/API.md` (HTTP and RTVI surface, error codes, limits).

## 1. Components

Repository map:

| Path | Contents |
|---|---|
| `relay/` | Production Python service, its tests and replay fixtures |
| `android/` | Gradle modules: `app`, `app/call`, `rtvi`, `wakeword`, `attachments`, plus the `stub-relay` fixture |
| `deploy/` | Compose stack, Caddy configuration, systemd unit |
| `docs/` | Architecture, ADRs, deployment, runbook, threat model, research records |
| `scripts/` | Repository hygiene gate, Hermes probes, router/speech/latency benchmarks, kanban seed |
| `spike/` | Prototypes kept for reference; not part of the shipped process |

Legend:

- **WIRED** — mounted on the FastAPI app (`relay/app.py`) or started by the deployment.
- **LIBRARY** — shipped and unit-tested, imported by callers and tests; not mounted directly on the
  app. The call loop (`relay/transport/call_loop.py`) composes these modules for RTVI sessions.
- **FIXTURE** — test-only, not part of the production process.

### 1.1 Relay (Python, `relay/`)

| Component | Path | Key symbols | Status |
|---|---|---|---|
| Service shell | `relay/app.py` | `create_app`, lifespan, `/`, `/health`, `/ready`, `/v1/whoami`, access middleware, session reaper | WIRED |
| Settings | `relay/config.py` | `Settings`, `get_settings` | WIRED |
| Device auth | `relay/auth/tokens.py`, `relay/auth/deps.py`, `relay/tokens.py` | `TokenStore`, `DeviceToken`, `require_device_token`, `python -m relay.tokens` CLI | WIRED |
| Admission and limits | `relay/limits.py` | `AdmissionController`, upload leases, per-device token buckets, `BargeInGate` | WIRED (app), LIBRARY (transport) |
| Attachment inbox | `relay/storage/inbox.py` | `InboxStore.ingest`, `sniff_mime`, `delegation_parts`, `sanitize_filename` | WIRED |
| Attachment validation | `relay/storage/validation.py`, `relay/storage/_probe.py` | isolated child process for PDF/image/zip parsing, `ValidationLimits` | WIRED |
| Artifact outbox | `relay/storage/outbox.py` | `OutboxStore.list_artifacts`, `open_artifact`, retention | WIRED |
| HTTP file API | `relay/api/attach.py`, `relay/api/files.py` | `POST/PUT /v1/attachments`, `GET/HEAD /v1/artifacts[/{id}]` | WIRED |
| Logging and audit | `relay/logging/` | `configure_logging`, `configure_redaction`, `scrub`, `audit`, `AuditLog`, retention and prune CLI | WIRED |
| Maintenance | `relay/maintenance/` | `python -m relay.maintenance prune`, Hermes session deletion | CLI |
| Turn detection and barge-in | `relay/transport/turns.py` | `TurnController`, `EnergyVad`, `EndOfTurnDetector`, `TtsPlayback`, `HttpRunStopper`, `BargeInGate` (from `relay/limits.py`) | LIBRARY (used by the call loop) |
| RTVI transport | `relay/transport/rtvi.py` | `RtviSessionManager`, `RtviSession`, `/start` and `/sessions/{id}` signalling, WebRTC audio bridge, `chat` data channel | WIRED |
| Call loop | `relay/transport/call_loop.py` | `CallLoop`, STT/VAD/turn pipeline, voice-brain dispatch, delegation narration, approval gate, barge-in, call cap | WIRED |
| Speech input | `relay/speech/stt.py`, `relay/speech/audio.py` | `create_stt`, `SherpaStreamingStt`, `WhisperFallbackStt`, `PcmChunk`, `resample_pcm16` | LIBRARY (used by the call loop) |
| Speech output | `relay/speech/tts.py`, `relay/speech/audio.py`, `relay/speech/backpressure.py` | `create_tts`, `create_tts_factory`, `StreamingTts`, `KokoroTtsEngine`, `OpusEncoder`, `BoundedAudioBuffer` | LIBRARY (used by the call loop) |
| Voice brain | `relay/brain/router.py`, `relay/brain/prompts.py`, `relay/brain/tools.json` | `VoiceBrainRouter.route`, `.from_settings`, `RouterConfig`, `ToolRegistry`, `DirectAnswerPolicy`, `SYSTEM_PREAMBLE`, `TOOL_ALLOWLIST` | LIBRARY (used by the call loop) |
| Delegation lifecycle | `relay/brain/delegation.py` | `DelegationService.delegate`, `delegation_input`, `envelope`, `DelegationState` | LIBRARY (used by the call loop) |
| Spoken approval gate | `relay/brain/approvals.py` | `ApprovalGate.request`, `.answer`, `.expire`, `Provenance`, `Decision` | LIBRARY (used by the call loop) |
| Tool narration | `relay/brain/narration.py` | `ToolNarrator.observe`, `phrase_for_tool`, `redact_spoken` | LIBRARY (used by the call loop) |
| Hermes adapter | `relay/hermes/client.py`, `relay/hermes/events.py` | `HermesClient.submit_run`, `.events`, `.get_run`, `.stop`, `.approval`, `SseDecoder`, `EventGate`, `EventKind` | LIBRARY (used by the call loop) |

The relay's HTTP surface served today is exactly:

```
GET  /                  service banner
GET  /health            liveness
GET  /ready             readiness (503 until settings, storage, auth, models and shutdown state pass)
GET  /v1/whoami         authenticated device identity
POST /v1/attachments    authenticated multipart upload
PUT  /v1/attachments    same handler, idempotent transport
GET  /v1/artifacts      authenticated artifact listing
GET  /v1/artifacts/{id} authenticated download (ETag, 304, HEAD)
HEAD /v1/artifacts/{id} metadata only
POST /start             authenticated RTVI session admission
POST /sessions/{id}/api/offer   authenticated SDP offer -> answer
PATCH /sessions/{id}/api/offer  authenticated trickle ICE
DELETE /sessions/{id}   unauthenticated RTVI session close (fixture contract parity)
```

`relay/limits.py::RATE_LIMIT_ROUTES` reserves `POST /v1/runs` for the relay-side run endpoint; the relay
app does not mount it. The call loop calls the gateway's `/v1/runs` through `relay/hermes/client.py`.

### 1.2 Android client (`android/`)

| Component | Path | Key symbols | Status |
|---|---|---|---|
| App shell | `android/app/src/main/java/com/example/hermestalk/` | `MainActivity`, `AppViewModel`, `SettingsRepository`, `ui/ConnectionScreen`, `ui/SettingsScreen` | WIRED |
| Call lifecycle | `android/app/call/src/main/java/com/example/hermestalk/call/` | `CallMachine`, `CallController`, `CallSession`, `CallForegroundService`, `AudioFocusController`, `CallAudioRoutePlanner`, `CallScreen` | WIRED |
| RTVI transport client | `android/rtvi/src/main/java/com/example/hermestalk/rtvi/` | `RtviSession.connect`, `RelayEndpoint.startUrl`, `HealthProbe`, `TlsTrust` (SPKI pinning), `TokenStore` (EncryptedSharedPreferences), `ConnectionMachine`, `SessionWatchdog` | WIRED |
| Wake word | `android/wakeword/src/main/java/com/example/hermestalk/wakeword/` | `WakeWordEngine`, `WakeWordMachine`, `PushToTalkController`, `DetectionGate`, sherpa-onnx KWS model under `android/app/src/main/assets/wakeword/` | WIRED |
| Persistent assistant | `android/app/call/src/main/java/com/example/hermestalk/call/` | `PersistentAssistantService`, `PersistentAssistant`, `PersistentAssistantController`, `AssistantWakeWordRunner`, `AssistantWakeWordPolicy`, `AssistantMicCoordinator`, `AssistantLaunch`, `BootReceiver` | WIRED |
| Attachments client | `android/attachments/src/main/java/com/example/hermestalk/attachments/` | `ShareIntent`, `Ingest`, `UploadQueueStore`, `UploadEngine`, `RetryPolicy`, `ArtifactClient`, `AttachmentPanel` | WIRED |
| Stub relay | `android/stub-relay/relay.py` | SmallWebRTC/RTVI signalling fixture: `/start`, `/sessions/{id}/api/offer`, trickle ICE, `DELETE /sessions/{id}` | FIXTURE |

`RtviSession` posts to `RelayEndpoint.startUrl()` (`<base>/start`) with a bearer header carrying the
device token, then answers the SDP offer on the session URL the relay returns. The production relay
mounts that surface in `relay/transport/rtvi.py`; media terminates in the relay process over
DTLS-SRTP. The Android instrumentation suite still exercises the same contract against
`android/stub-relay/relay.py`. The compose file does not publish a UDP port (`docs/DEPLOYMENT.md`,
port table), so a device-to-production-relay call over the proxy is not claimed yet.

### 1.3 Deployment (`deploy/`)

| Component | Path | Purpose |
|---|---|---|
| Reverse proxy | `deploy/Caddyfile` | TLS from Caddy's internal CA on `:443`, HTTP→HTTPS on `:80`, `reverse_proxy 127.0.0.1:8790` with `flush_interval -1`, no compression, HSTS and `nosniff` |
| Compose stack | `deploy/compose.yaml` | `volume-init` one-shot, relay with host networking and a read-only rootfs, Caddy; secrets only through `.env` |
| systemd unit | `deploy/hermes-talk.service` | alternative to compose, binds loopback only, `ProtectSystem=strict`, `ProtectHome=yes`, `StateDirectory=hermes-talk` |

### 1.4 External processes (not in this repository)

| Service | Default bind | Consumed by | Notes |
|---|---|---|---|
| Hermes API server | `127.0.0.1:8642` | `relay/hermes/client.py` | bearer token, `/v1/runs`, `/v1/runs/{id}`, `/v1/runs/{id}/events`, `/v1/runs/{id}/stop`, `/api/sessions/*` |
| Voice-brain model server | `127.0.0.1:8080` | `relay/brain/router.py` | OpenAI chat-completions with tool calls; `--jinja` chat template |
| Auxiliary inference | `127.0.0.1:8081` | operator tooling | LocalAI-compatible endpoints |
| Speech models | local files | `relay/speech/*` | sherpa-onnx streaming transducer, faster-whisper fallback, Kokoro ONNX voice pack |

## 2. Topology and trust boundaries

```
                 LAN or tailnet (no internet dependency)
 ┌───────────────────────────┐
 │ Android client            │
 │  app/  app/call/          │
 │  rtvi/  wakeword/         │
 │  attachments/             │
 └────┬─────────────────┬────┘
      │ TB1             │ TB1m
      │ HTTPS 443       │ DTLS-SRTP media (UDP)
      ▼                 ▼
 ┌──────────────┐   ┌──────────────────────────────┐
 │ Caddy        │   │ WebRTC endpoint              │
 │ :443 / :80   │   │  SmallWebRTC signalling      │
 │ internal CA  │   │  (/start, offer, trickle)    │
 └──────┬───────┘   │  served today by the stub    │
        │ TB2       │  relay (android/stub-relay)  │
        │ loopback └──────────────┬───────────────┘
        ▼                         │ audio frames (when mounted)
 ┌────────────────────────────────────────────────────┐
 │ relay  127.0.0.1:8790                              │
 │  app.py  api/*  auth/*  limits.py  storage/*       │
 │  logging/*  maintenance/*                          │
 │  transport/turns.py  speech/*  brain/*  hermes/*   │
 │  state: INBOX_DIR, OUTBOX_DIR, DATA_DIR,           │
 │         TOKEN_STATE_PATH (outside the repo)        │
 └───┬──────────────┬───────────────┬─────────────────┘
     │ TB3          │ TB4           │ TB5
     │ loopback     │ loopback      │ loopback
     ▼              ▼               ▼
 Hermes API     voice-brain      LocalAI
 :8642          :8080            :8081
     │
     ▼
 agent loop + tools (host filesystem, shell, network policy of the agent)
```

| Boundary | What crosses | Control | Enforced in |
|---|---|---|---|
| **TB1** phone → proxy | HTTP requests, bearer device token, attachment bytes, artifact downloads | TLS from the internal CA, SPKI pin on the device, bearer `Authorization`, 401 with `WWW-Authenticate`, admission limits | `deploy/Caddyfile`, `relay/auth/deps.py`, `relay/auth/tokens.py`, `android/rtvi/TlsTrust.kt`, `relay/limits.py` |
| **TB1m** phone ⇄ WebRTC endpoint | microphone PCM, Opus, RTVI data messages | DTLS-SRTP, bearer token on `/start`, SDP/ICE validated by the endpoint, session admission and call cap | `relay/transport/rtvi.py`, `relay/transport/call_loop.py`, `relay/transport/turns.py`; `android/stub-relay/relay.py` remains the Android instrumentation fixture |
| **TB2** proxy → relay | plain HTTP on loopback | relay binds `127.0.0.1` only; the proxy is the sole LAN listener; no secret in the URL | `relay/config.py` (`relay_host`), `deploy/compose.yaml`, `deploy/hermes-talk.service` |
| **TB3** relay → Hermes | run input, session key, bearer token, SSE events | loopback only, one bearer token, session key validated locally, response bodies validated against the documented envelope, typed errors | `relay/hermes/client.py`, `relay/hermes/events.py`, `relay/config.py` |
| **TB4** relay → voice brain | utterance text, tool schemas | loopback only, optional bearer key, allowlist validation of every returned tool name and argument | `relay/brain/router.py`, `relay/brain/tools.json` |
| **TB5** relay → inference | model requests | loopback only; no authentication by default on the local servers | `relay/config.py` |
| **TB6** untrusted content → model | attachment bytes, tool output, documents, model text | magic-byte sniffing, isolated parse child, caps, provenance envelopes with nonce, spoken-only approval | `relay/storage/*`, `relay/brain/prompts.py`, `relay/brain/delegation.py`, `relay/brain/approvals.py` |
| **TB7** process → disk and logs | transcripts, audit records, optional recordings | redaction registry, hash-only token state, `0700` roots outside the repo, retention pruning | `relay/logging/*`, `relay/auth/tokens.py`, `relay/maintenance/*` |
| **TB8** repository → public | every tracked byte | `scripts/check_repo_hygiene.sh` rules R1–R7 and gitleaks in CI | `.github/workflows/ci.yml`, `.pre-commit-config.yaml` |

The threat model enumerates the attacks each boundary is meant to stop; this table is the map from
boundary to code, not a replacement for `docs/security/threat-model.md`.

## 3. Data flow

### 3.1 Voice turn

```
 wake word / push-to-talk
   android/wakeword  WakeWordEngine -> WakeWordMachine -> CallMachine
   android/rtvi      RtviSession.connect -> POST <base>/start -> SDP offer
   android/app/call  CallForegroundService + AudioFocusController own the mic

 mic PCM (16 kHz mono PCM16)
   relay/transport/turns.py  TurnController.feed_mic -> EnergyVad -> EndOfTurnDetector
   relay/speech/stt.py       SherpaStreamingStt partials and finals (no network)

 final transcript
   relay/brain/router.py     VoiceBrainRouter.route
     DirectAnswerPolicy hit -> spoken text or ack, then tool_call / done   (no HTTP)
     otherwise -> POST <VOICE_BRAIN_BASE_URL>/v1/chat/completions with tools.json
   tool_call delegate_to_hermes
     relay/brain/delegation.py DelegationService.delegate
     relay/hermes/client.py    HermesClient.submit_run -> POST /v1/runs (202, run_id)
     spoken ack emitted before the run is awaited

 background watcher
   relay/hermes/client.py    HermesClient.events -> relay/hermes/events.py SseDecoder
                             -> EventGate (seq order, de-duplication, one terminal)
   relay/brain/narration.py  ToolNarrator.observe -> mapped phrase, throttled 4 s
   relay/speech/tts.py       StreamingTts -> Kokoro -> Opus 48 kHz
   relay/brain/delegation.py exactly one SpokenUpdate: answer, spoken failure, or cancellation

 barge-in
   relay/transport/turns.py  TurnController -> BargeInGate -> HttpRunStopper
   relay/hermes/client.py    HermesClient.stop -> POST /v1/runs/{id}/stop
```

Ordering guarantees that the code relies on:

1. `delegate()` awaits only admission. The spoken ack is produced by `VoiceBrainRouter` when the tool
   name is known, before the Hermes call is awaited.
2. The watcher ignores non-terminal events for delivery; narration is a separate, throttled path that
   reads only the tool name from `tool.started`.
3. A terminal event is delivered exactly once per delegation record (`delivered` flag set before any
   await); stop and watcher paths share that gate.
4. Barge-in stops the run and writes an interruption marker
   (`INTERRUPTION_MARKER_TEMPLATE`) so the next turn does not repeat the cut-off text.

### 3.2 Attachment upload and delegation

```
 share intent / picker
   android/attachments  ShareIntent -> Ingest -> UploadQueueStore -> UploadEngine
   PUT /v1/attachments  multipart file, bearer Authorization header

 relay/app.py -> relay/auth/deps.py require_device_token
   -> relay/limits.py  acquire_upload (lease, queue cap, per-device bucket)
   -> relay/storage/inbox.py InboxStore.ingest
        magic-byte sniff (relay/storage/inbox.py sniff_mime)
        structural parse in relay/storage/_probe.py child process
          (10 s wall clock, 1 GiB address space, minimal environment)
        atomic write under INBOX_DIR (0700), server-generated id, recorded sha256
   -> relay/logging/audit.py audit_attachment (accepted or rejected, reason code)
   -> 201 {"attachment": {...}, "spoken": "Picture saved."}

 delegation
   relay/brain/delegation.py delegation_input(utterance, attachments, store)
     text part: "Attached file i/n: path=... sha256=... mime=... bytes=... name=..."
     image part: data URL when the image is within the inline budget
     excerpt part: <<<UNTRUSTED:document name=... sha256=... nonce=...>>> ... <<<END:nonce>>>
   relay/storage/inbox.py delegation_parts verifies the stored hash before every hand-off
```

`DelegationService.delegate()` submits the `delegation_input()` content array to `POST /v1/runs`
when attachments are supplied: a text part per attachment with its path, hash, MIME type, size and
name, an inline `image_url` data URL for images, and an optional untrusted excerpt envelope. With no
attachments it submits a plain string instruction. `delegation_parts` re-verifies the stored sha256
before every hand-off; if the bytes no longer match, the delegation fails with the spoken failure and
nothing is submitted to the gateway.

### 3.3 Artifact download

```
 agent writes a file under OUTBOX_DIR (outside the repository)
   GET /v1/artifacts        OutboxStore.list_artifacts (unexpired only, limit 1..200)
   GET /v1/artifacts/{id}   OutboxStore.open_artifact, 64 KiB chunks,
                            ETag = sha256, If-None-Match -> 304, HEAD -> metadata only
   relay/logging/audit.py   artifact.served / artifact.rejected
```

### 3.4 Approval and clarify

```
 relay/hermes/events.py   approval.request / clarify.request -> normalized kinds
 relay/brain/approvals.py ApprovalGate.request -> spoken prompt
                          ApprovalGate.answer -> Decision, only Provenance.SPOKEN,
                          minimum confidence 0.6, negation wins over the phrase
                          ApprovalGate.expire -> deny on timeout (default 20 s)
 relay/logging/audit.py   audit_approval_requested / audit_approval_decided
```

The gate never reads tool output as consent, and narration can never approve. The decision carries
`prompt_id` and `execution_generation`, which are exactly the fields the gateway's approval endpoint
requires (section 5.5). `HermesClient.approval(run_id, request_id=..., execution_generation=...,
choice=...)` posts exactly those three keys to `POST /v1/runs/{run_id}/approval` and tolerates a `409`
(run already settled); the adapter also reads `pending_controls` from `GET /v1/runs/{id}`.

### 3.5 Persistent assistant service lifecycle

The always-on assistant is a second foreground service in `:app:call`, separate from
`CallForegroundService`, which stays call-scoped. It is opt-in: the plain preference
`hermes_talk_persistent` / `assistant_enabled` (default `false`) is written by the Settings toggle and
read by `BootReceiver`. Plain rather than encrypted storage is deliberate — the boot receiver runs
before the user unlocks, so `LOCKED_BOOT_COMPLETED` is not handled.

```
 disabled --toggle on--> Starting(foreground) --mic type accepted--> Foreground(Microphone)
                                    \--mic refused--> Foreground(SpecialUse)
 Foreground(Microphone) --wake word enabled--> Listening (partial wake lock held)
 Listening --call starts--> listening=false (mic owned by CallForegroundService, lock released)
 listening=false --call ends--> Foreground(Microphone) / Listening
 any state --toggle off / ACTION_STOP--> Stopped (notification removed, lock released)
 any state --process killed--> START_STICKY restart
 BOOT_COMPLETED (opt-in) --> Starting(background) --mic refused--> Foreground(SpecialUse)
 Foreground(SpecialUse) --app visible / ACTION_PROMOTE_MIC--> Foreground(Microphone)
```

Start paths are `PersistentAssistantController.startFromForeground` (Settings toggle and app resume),
`promoteToMicrophone` (`ACTION_PROMOTE_MIC` on resume) and `BootReceiver` (`BOOT_COMPLETED`,
`MY_PACKAGE_REPLACED`). `PersistentAssistant.attemptOrder` returns the foreground types to try in order:
no typed start below API 29, otherwise microphone then special use when `RECORD_AUDIO` is granted, and
special use alone without it. `PersistentAssistantService` calls `startForeground` on every
`onStartCommand`, records a rejected microphone attempt in `PersistentAssistant.state.micRefusal`, and
retries special use inside the same call so the five-second window is not missed. The service returns
`START_STICKY`, keeps running in `onTaskRemoved`, declares `stopWithTask="false"`, and releases its
wake lock in `onDestroy`.

The microphone has exactly one owner at a time. `AssistantWakeWordPolicy.micOwner` grants capture to
the wake word only when a call is not live, the wake-word and assistant toggles are on, `RECORD_AUDIO`
is granted and the service holds the microphone foreground type; a live call wins unconditionally.
`ProcessAssistantMicCoordinator.suspendWakeWord` stops capture before the call service starts and
`resumeWakeWord` restarts it after the call ends. The partial wake lock
(`hermes_talk:assistant-listening`) is non-reference-counted, has no timeout, and is held only while the
controller reports `Listening`; every other state, and every live call, releases it.

A detection runs the fixed `AssistantDetectionStep` sequence — stop capture, release the listening wake
lock, abandon audio focus, clear the communication device, leave communication mode, launch
`ACTION_ASSIST` — and `AssistantWakeWordPolicy.DETECTION_TO_CALL_BUDGET_MILLIS` bounds the measured
detection-to-call-screen time at 1000 ms.

The platform constrains the background path. API 34 refuses a microphone foreground service started
from `BOOT_COMPLETED` (`ForegroundServiceStartNotAllowedException` per the release notes; the API 34
image raised `SecurityException`), so a boot-started service holds `specialUse` and wake-word capture
stays paused until the app is visible and promotes it to `microphone`. A foreground service is also not
a background-activity-launch exemption, so `AssistantLaunch` may fail to open the call screen from the
background. The platform usually blocks such a launch silently (no exception; the block appears only in
logcat), so `AssistantLaunchResult.started` means only that `startActivity` was accepted without
throwing — it is not evidence that the screen opened. The foreground notification's content intent is
the one-tap fallback.

Observability is in-process only: `PersistentAssistant.state` exposes `running`, `foregroundType`,
`listening`, `wakeLockHeld`, `micRefusal` and `startedAtMillis`; the Settings screen renders
`Running (microphone)`, `Running (special use)`, `Listening` or `Not running` and shows the recorded
refusal. No audio, transcript or state leaves the device on this path. `docs/RUNBOOK.md` section 12 has
the operator commands and `docs/TESTING.md` the measured soak, swipe and reboot results.

## 4. Relay ↔ Hermes contract

The authoritative record of observed gateway behaviour is `docs/research/hermes-api.md`. The adapter
implements the subset below; examples are recorded responses from that document, with tokens masked.

### 4.1 Headers and identity

```
Authorization: Bearer <HERMES_API_KEY>
Content-Type: application/json
X-Hermes-Session-Key: <HERMES_SESSION_KEY>        optional, validated locally
Idempotency-Key: dlg-<32 hex>                    delegation submissions only
Accept: text/event-stream                        event stream only
```

`HermesClient.__init__` accepts `http`/`https` base URLs only, requires a non-empty key, and rejects
non-finite or non-positive timeouts. `_validated_session_key` accepts printable ASCII of 1–256
characters before any request is built; neither the token nor the session key appears in exception
messages.

Session semantics: with a declared session key the turn lands in the session already bound to that
key, so `run_id != session_id`. The adapter never assumes equality; it reads `session_id` back from
`GET /v1/runs/{id}`. An unknown `session_id` is adopted by the gateway (`source: api_server`); a
session owned by another surface is refused with `409` (section 10 of the research document).

### 4.2 Submit a run — `POST /v1/runs`

Request body built by `HermesClient.submit_run`:

```json
{"input": "Use your terminal tool to run the shell command `uname -s`, then reply with only the output.",
 "session_id": "voice-session-0001"}
```

Recorded admission:

```
HTTP/1.1 202 Accepted
X-Hermes-Session-Key: voice-relay-probe
{"run_id": "run_1a360b36daf6412f9757d2bdf5b918b1", "status": "started", "replayed": false}
```

An identical retry under the same `Idempotency-Key` replays the original run and reports the current
status plus `Idempotency-Replayed: true`. A different payload under the same key is
`409 idempotency_key_conflict`, which `HermesClient` maps to `HermesConflictError` and never retries.
`DelegationService` holds one `asyncio.Lock` per key around check-and-submit, so two callers cannot
both admit a run.

### 4.3 Event stream — `GET /v1/runs/{id}/events`

Recorded transcript of a tool-using turn (trimmed to the event frames):

```
: open

: keepalive

id: 0
data: {"event": "tool.started", "run_id": "run_1a360b36daf6412f9757d2bdf5b918b1", "timestamp": 1793615952.9138777, "tool": "terminal", "preview": "uname -s", "tool_call_id": "call_305c3106aab2477a9b0c1f1e", "seq": 0}

id: 1
data: {"event": "tool.completed", "run_id": "run_1a360b36daf6412f9757d2bdf5b918b1", "timestamp": 1793615953.090746, "tool": "terminal", "duration": 0, "error": false, "tool_call_id": "call_305c3106aab2477a9b0c1f1e", "preview": "", "seq": 1}

id: 2
data: {"event": "message.delta", "run_id": "run_1a360b36daf6412f9757d2bdf5b918b1", "timestamp": 1793615962.3202968, "delta": "\n\nLinux", "seq": 2}

id: 3
data: {"event": "run.completed", "run_id": "run_1a360b36daf6412f9757d2bdf5b918b1", "timestamp": 1793615962.4686923, "output": "Linux", "usage": {"input_tokens": 41321, "output_tokens": 44, "total_tokens": 41365}, "runtime": {"provider": "", "model": "", "route_source": "global"}, "completed": true, "partial": false, "interrupted": false, "seq": 3}

: stream closed
```

`relay/hermes/events.py` maps the gateway's vocabulary onto `EventKind`:

| Gateway name | `EventKind` | Adapter action |
|---|---|---|
| `tool.started`, `tool.start` | `TOOL_STARTED` | narration phrase for the tool name |
| `tool.completed`, `tool.complete` | `TOOL_COMPLETED` | no spoken output by default |
| `message.delta`, `message.interim` | `MESSAGE_INTERIM` | ignored by the watcher |
| `run.completed` | `RUN_COMPLETED` | terminal; spoken answer |
| `run.cancelled` | `RUN_CANCELLED` | terminal; spoken cancellation |
| `run.failed` | `RUN_FAILED` | terminal; spoken failure |
| `approval.request` | `APPROVAL_REQUEST` | approval gate |
| `approval.settled`, `approval.responded` | `APPROVAL_SETTLED` | clears the gate |
| `clarify.request` | `CLARIFY_REQUEST` | clarify prompt |
| `replay.truncated` | `REPLAY_TRUNCATED` | tolerated; the stream continues, and a stream that ends without a terminal falls back to status |
| anything else | `UNKNOWN` | yielded, never fatal |

`EventGate` enforces monotonically increasing `seq`, suppresses duplicates, permits exactly one
terminal event (including across reconnects), and raises a protocol error on a foreign `run_id`.

**Resume and reconnect.** The initial resume request uses `?last_seq=N`; every reconnect uses
`Last-Event-ID: N`. Reconnects are attempted for read errors, protocol errors, timeouts and connect
failures with capped exponential backoff (50 ms doubling, capped at 1 s, at most 3 attempts). When
they are exhausted the adapter calls `GET /v1/runs/{id}` and synthesises one terminal event from a
terminal status; otherwise it raises `HermesStreamError`.

A swept event buffer answers `404 run_not_found` while the status endpoint still returns the terminal
body. That case is `HermesRunGoneError` (a subclass of `HermesNotFoundError`), so the caller can still
read the outcome. Do not treat it as a lost run.

### 4.4 Stop — `POST /v1/runs/{id}/stop`

`HermesClient.stop` posts `{}` with a 2 s request timeout and measures elapsed time:

```
{"run_id": "run_abd3b2c9ddfb447b80387aa07024aa91", "status": "stopping", "admission_id": "b4e0f7b78c8c45d2b342d6f1c813f068"}
```

The run then settles as `cancelled`; the stream emits:

```
id: 0
data: {"event": "run.cancelled", "run_id": "run_abd3b2c9ddfb447b80387aa07024aa91", "timestamp": 1793616035.1420393, "completed": false, "partial": false, "interrupted": true, "turn_exit_reason": "interrupted_during_api_call", "seq": 0}
```

`run.cancelled` with `interrupted: true` is the relay's own requested outcome, not a failure.
Stopping an already-terminal run returns the terminal body with `200`. `HttpRunStopper` in
`relay/transport/turns.py` calls the same endpoint for barge-in.

### 4.5 Status, pending controls, approval and clarify

`GET /v1/runs/{id}` returns `object`, `model`, `runtime`, `created_at`, `pending_controls`, `run_id`,
`status`, `session_id`, `admission_id`, `execution_generation`, `output`, `usage`. Recorded pending
approval:

```json
{
  "pending_controls": [
    {"kind": "approval", "prompt_id": "a8ff8ef17ed04d718fcd6bc9e4cd3210", "execution_generation": 1,
     "command": "rm -rf /tmp/hermes-api-probe-dir", "description": "delete in root path",
     "choices": ["once", "deny", "session", "always"]}
  ],
  "run_id": "run_d356f9d7be3f4a18a5fc65caa0768db3",
  "status": "waiting_for_approval",
  "session_id": "run_d356f9d7be3f4a18a5fc65caa0768db3",
  "execution_generation": 1,
  "output": "",
  "usage": {}
}
```

A pending clarify does **not** change `status` (it stays `running`); the adapter must watch
`pending_controls` or the `clarify.request` event. `execution_generation` is opaque: echo it back
unchanged.

Answering an approval (`POST /v1/runs/{run_id}/approval`) accepts exactly three fields:

```json
{"request_id": "a8ff8ef17ed04d718fcd6bc9e4cd3210", "execution_generation": 1, "choice": "once"}
```

```
HTTP/1.1 200 OK
{"run_id": "run_d356f9d7be3f4a18a5fc65caa0768db3", "status": "resolved", "prompt_id": "a8ff8ef17ed04d718fcd6bc9e4cd3210"}
```

`choice` is one of `once`, `session`, `always`, `deny`. Extra keys are `400 invalid_params`; a stale
generation or an already-answered prompt is `409`. `approval_not_pending` on a retry means the prompt
was already answered and must not be surfaced as an error. Clarify answers go to
`POST /v1/runs/{run_id}/clarify` with the same `request_id`/`execution_generation` fields plus a
free-text `answer`; `choice` in that body is `400 invalid_params`.

### 4.6 Error envelope and typed mapping

Every gateway error is `{"error": {"message", "type", "param", "code"}}`. The adapter parses the code
and never echoes the body into a spoken string.

| Gateway condition | Adapter exception | Retry |
|---|---|---|
| `401` | `HermesAuthError` | no |
| `404` on `/events` with `run_not_found` | `HermesRunGoneError` | no, read status |
| other `404` | `HermesNotFoundError` | no |
| `409` (conflict, permission, idempotency) | `HermesConflictError` | no |
| connect/read failure | `HermesUnavailableError` | stream reconnects; otherwise no |
| timeout | `HermesTimeoutError` | stream reconnects; otherwise no |
| envelope or gate violation | `HermesProtocolError` | no |
| no terminal after reconnects and a status fallback | `HermesStreamError` | no |

## 5. Voice brain and delegation contract

### 5.1 Router

`VoiceBrainRouter.route(utterance, attachments_pending=...)` yields `ack`, `text_delta`,
`text_final`, `tool_call` and `done` events. Request body fields: `model`, `messages`
(`router_system_message` + user utterance), `tools` (`ToolRegistry.schemas`), `tool_choice`,
`temperature`, `max_tokens`, `stream`, `stream_options`, `chat_template_kwargs`. Configuration comes
from `VOICE_BRAIN_*` in `.env.example`; eager exposure of all four tools is the default
(`VOICE_BRAIN_DEFERRED_TOOLS=0`).

| Tool | Purpose | Arguments |
|---|---|---|
| `delegate_to_hermes` | hand work to the agent, return immediately | `instruction` |
| `answer_direct` | reply without the agent | `text` |
| `attach_file` | attach a file the user just sent | `filename` |
| `end_call` | end the conversation | none |

`TOOL_ALLOWLIST` in `relay/brain/prompts.py` is the single source of truth; `ToolRegistry` validates
the JSON schema on load and `VoiceBrainRouter` rejects any tool name or argument outside it. Unknown
names and malformed or oversized arguments are protocol errors; only a missing `instruction` or
`filename` is repaired.

`DirectAnswerPolicy` answers time, date, arithmetic, presence checks and terminal farewells without a
model call. Everything else goes to the model; `answer_direct` is the fallback for small talk.

### 5.2 Delegation lifecycle

```
delegate(instruction, idempotency_key)
  -> per-key lock -> existing record? return it (deduplicated=True)
  -> submit_run -> 202 admission -> RUNNING, run_id recorded, watcher started
       -> tool.started  -> narration (throttled)
       -> run.completed -> COMPLETED, spoken answer
       -> run.failed    -> FAILED, spoken failure
       -> stop accepted -> CANCELLED, spoken cancellation
       -> stream error  -> spoken failure, never a silent death
```

Records are bounded (256) and retained for 3600 s; the terminal delivery flag is set before any await
so a stop racing a terminal cannot produce two updates. Spoken answers are sanitised with
`prompts.spoken_text`, secret redaction and a home-prefix rewrite (`/home/<user>` and
`/Users/<user>` become `~`); narration additionally runs `relay.logging.redact.scrub`. No personal
path is spoken or logged.

### 5.3 Containment

`CONTAINMENT_RULE`, `ATTACHMENT_RULE` and the envelope templates are shared verbatim by the voice
brain and the delegation preamble. Envelopes carry a nonce and a matching close tag; content inside an
envelope is data and can never approve a tool call. `ApprovalGate` accepts only
`Provenance.SPOKEN` utterances above the confidence floor; everything else is `None`, which the caller
treats as "still pending" until the timeout denies it.

## 6. Deployment and local-only invariant

Ports and binds are listed in `docs/DEPLOYMENT.md`; the load-bearing facts are:

- The relay binds `127.0.0.1:8790`. Caddy is the only LAN listener (`:443`, `:80`).
- Hermes (`:8642`), the voice brain (`:8080`), LocalAI (`:8081`) and the Caddy admin API (`:2019`)
  are loopback-only.
- Compose publishes no UDP port. WebRTC media belongs to the signalling endpoint, not to the proxy.
- Every model path is mounted read-only from outside the repository; weights are never committed.
- The relay runs as uid 10001 with `read_only`, `cap_drop: ALL`, `no-new-privileges`, and a `tmpfs`
  `/tmp` that permits `exec` because phonemizer `dlopen()`s a copied shared object.
- `/ready` is `503` until settings, storage, auth, models and shutdown state pass; the compose
  healthcheck probes `/health` and `/ready` together.

No runtime component may reach the internet (ADR 0001). The only permitted network calls are loopback
HTTP to Hermes and the local model servers; `scripts/check_repo_hygiene.sh` and gitleaks keep the
repository itself free of personal paths, LAN addresses and secrets.

## 7. Where to change what

| Task | Files | Tests |
|---|---|---|
| Add or change an HTTP route | `relay/app.py`, `relay/api/*`, `relay/limits.py` route table | `relay/tests/test_health.py`, `test_files_api.py`, `test_attach_api.py`, `test_limits.py` |
| Change attachment validation | `relay/storage/inbox.py`, `relay/storage/validation.py`, `relay/storage/_probe.py` | `test_attachments.py`, `test_attachment_bombs.py`, `test_injection.py` |
| Change device auth | `relay/auth/*`, `relay/tokens.py` | `test_tokens.py` |
| Change turn detection or barge-in | `relay/transport/turns.py`, `relay/limits.py` | `test_barge_in.py` |
| Change speech adapters | `relay/speech/*` | `test_speech_stt.py`, `test_speech_tts.py`, `test_speech_audio.py`, `test_speech_config.py`, `test_bench_speech.py` |
| Change the router prompt or tools | `relay/brain/prompts.py`, `relay/brain/tools.json`, `relay/brain/router.py` | `test_brain_router.py`, `scripts/bench_router.py` |
| Change delegation | `relay/brain/delegation.py` | `test_delegation.py`, `test_attachments.py` |
| Change approval policy | `relay/brain/approvals.py` | `test_injection.py` |
| Change narration | `relay/brain/narration.py` | `test_narration.py`, `test_logging_redaction.py` |
| Change the Hermes adapter | `relay/hermes/*` | `test_hermes_adapter.py`, `test_runs.py`, fixtures under `relay/tests/fixtures/` |
| Change retention | `relay/maintenance/*`, `relay/logging/retention.py` | `test_retention.py` |
| Change the Android call flow | `android/app/call/*`, `android/rtvi/*` | `android/*/src/test/`, instrumentation suite |
| Change the persistent assistant or wake-word capture | `android/app/call/PersistentAssistant*.kt`, `AssistantWakeWord*.kt`, `AssistantMicCoordinator.kt`, `AssistantLaunch.kt`, `BootReceiver.kt` | `android/app/call/src/test/`, `PersistentAssistantServiceTest`, `AssistantWakeWordServiceTest` |
| Change the RTVI signalling fixture | `android/stub-relay/relay.py` | `android/rtvi/src/test/` |

Fixtures are synthetic and committed under `relay/tests/fixtures/`; the Hermes replay stub is
`relay/tests/_hermes_stub.py`. Run the suite from the repository root (`python -m pytest relay/tests`)
because `relay/logging` shadows the stdlib `logging` package when the working directory is `relay/`.

## 8. Invariants

1. **Local-only.** No runtime path depends on an internet service; a cloud backend would need an ADR.
2. **Deny by default.** An unanswered approval expires into `deny`; only spoken confirmation approves.
3. **Exactly one terminal.** A delegation delivers one spoken outcome; the event gate allows one
   terminal event per run, including across reconnects.
4. **Untrusted content stays data.** Envelopes, provenance and the isolated parser keep attachment
   bytes and tool output out of the instruction channel.
5. **Bounded everything.** Audio queues, SSE frame budgets, delegation records and per-device buckets
   have explicit caps in `relay/limits.py`, `relay/speech/backpressure.py` and
   `relay/brain/delegation.py`; overflow is a typed error with a spoken sentence, never silent loss.
6. **No secrets or personal paths.** Tokens, session keys and API keys are redacted at the logging
   boundary; spoken text rewrites home prefixes; the repository contains neither.
7. **Loopback only.** The relay and every model server bind `127.0.0.1`; the proxy is the sole LAN
   entry point.
8. **One microphone owner.** A live call owns the microphone unconditionally; wake-word capture stops
   before the call service starts and resumes only after the call ends and the service holds the
   microphone foreground type.
