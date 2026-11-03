#!/usr/bin/env python3
"""Seed the Hermes Talk kanban board.

Single source of truth for the project backlog. Idempotent: re-running returns
existing ids for unchanged keys and never duplicates cards.

Usage:
    python3 scripts/seed_kanban.py                # seed (epics parked, tasks todo)
    python3 scripts/seed_kanban.py --dry-run      # print the graph, create nothing

Board: hermes-talk   Repo: /store/archive/hermes-talk

Standing constraints baked into every card:
  * local-only at runtime (no cloud STT/TTS/LLM)
  * public repo: no PII, secrets, home paths, or hard-coded LAN addresses
  * never restart services without the user's explicit go; block with needs_input
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

BOARD = "hermes-talk"
WORKDIR = "/store/archive/hermes-talk"

CONSTRAINTS = (
    "**Constraints:** local-only at runtime (no cloud STT/TTS/LLM); this repo is public — "
    "no personal data, secrets, /home paths, or hard-coded LAN addresses; never restart a "
    "service without the user's go (block with needs_input instead); work in "
    f"`{WORKDIR}` and keep the tree clean."
)

EPICS = [
    (
        "E0",
        "EPIC P0 — Ground truth and feasibility",
        (
            "Establish what the stack can actually do before writing product code: model benchmarks, the Hermes "
            "API contract, the Android stack choice, and the threat model.\n\n"
            "Exit: every P0 card done; findings recorded under docs/research/ and docs/security/."
        ),
    ),
    (
        "E1",
        "EPIC P1 — Text relay",
        (
            "A relay service that sends a prompt to Hermes and streams the answer, with stop and session "
            "continuity. No audio yet.\n\nExit: integration tests green against a stubbed Hermes API."
        ),
    ),
    (
        "E2",
        "EPIC P2 — Local voice loop",
        (
            "Speech in, speech out, fully on-prem: streaming STT, an LFM tool-router, streaming TTS, and "
            "turn-taking with barge-in.\n\nExit: 20 scripted utterances under 1.2 s perceived latency."
        ),
    ),
    (
        "E3",
        "EPIC P3 — Delegation, narration, approvals",
        (
            "The voice brain delegates real work to Hermes, narrates tool activity, and gates destructive "
            "actions behind a spoken confirmation.\n\nExit: destructive command denied on silence."
        ),
    ),
    (
        "E4",
        "EPIC P4 — Attachments and artifacts",
        (
            "Files in (images, documents, audio) and files out (generated artifacts), stored outside the repo "
            "and injected by path.\n\nExit: image edit and PDF analysis end-to-end."
        ),
    ),
    (
        "E5",
        "EPIC P5 — Android client",
        (
            "Native Kotlin/Compose app: call UI, foreground microphone service, on-device wake word, "
            "share-sheet, and artifact viewer.\n\nExit: screen-off call, wake word, 30-minute session."
        ),
    ),
    (
        "E6",
        "EPIC P6 — Hardening, documentation, release",
        (
            "Security gates, docs, deploy draft, code review, and the v0.1 checklist.\n\nExit: tagged release "
            "with green CI and a clean public repo."
        ),
    ),
]

# key, phase, title, assignee, parents, body
TASKS = [
    # ---------------- P0 ----------------
    (
        "p0-models",
        "P0",
        "Benchmark local voice-brain candidates",
        "researcher",
        [],
        (
            "**Objective:** measure whether a local model can drive a real-time conversation with tool calls.\n\n"
            "**Candidates:** `lfm2.5-audio-1.5b-realtime` (already served locally), `LFM3-Audio-2B` (audio "
            "function calling), `LFM3-1.7B` / `LFM2.5-1.2B-Tool` as a text router, and Moshi as a full-duplex "
            "reference.\n\n"
            "**Deliverables:** `docs/research/voice-models.md` with a table of VRAM footprint, first-token "
            "latency, tokens/s, tool-call accuracy on 20 scripted utterances, and licence terms.\n\n"
            "**Acceptance:** numbers measured on the relay host, not quoted from vendor pages; a clear "
            "recommendation for Path A (audio-native) vs Path B (cascade), including the VRAM budget when the "
            "GPU is also serving image generation."
        ),
    ),
    (
        "p0-api",
        "P0",
        "Document the Hermes API contract for the relay",
        "researcher",
        [],
        (
            "**Objective:** pin the exact endpoints, payloads, and failure modes the relay will depend on.\n\n"
            "**Cover:** `POST /v1/runs`, `GET /v1/runs/{id}/events` (event names and ordering), "
            "`POST /v1/runs/{id}/stop`, `GET /v1/runs/{id}` pending controls, approval and clarify responses, "
            "`/api/sessions/*`, `X-Hermes-Session-Key`, and `/v1/capabilities`.\n\n"
            "**Verify explicitly:** uploaded files are rejected (inline images only), and a REST request cannot "
            "bind a session owned by another surface (409 `permission_denied`).\n\n"
            "**Deliverables:** `docs/research/hermes-api.md` with copy-pasteable curl transcripts and the exact "
            "error bodies.\n\n"
            "**Acceptance:** a reader can implement the adapter from the document alone; every claimed behaviour "
            "has an observed response."
        ),
    ),
    (
        "p0-android",
        "P0",
        "Android stack spike: Pipecat RTVI vs LiveKit",
        "researcher",
        [],
        (
            "**Objective:** choose the client transport and prove it on a real device.\n\n"
            "**Compare:** Pipecat Android SDK with SmallWebRTC transport vs the LiveKit Android starter app; "
            "on-device keyword spotting with sherpa-onnx; Android 14+ foreground microphone service rules and "
            "battery-optimisation exemptions; Bluetooth headset routing.\n\n"
            "**Deliverables:** `docs/research/android-stack.md` plus a throwaway demo app that connects to a "
            "stub relay and plays a tone both ways.\n\n"
            "**Acceptance:** demo runs with the screen off for 10 minutes; recommendation includes the "
            "permission manifest and the wake-word engine."
        ),
    ),
    (
        "p0-threat",
        "P0",
        "Threat model and privacy review",
        "security-auditor",
        [],
        (
            "**Objective:** enumerate what can go wrong before code exists.\n\n"
            "**Cover:** trust boundaries (phone, relay, Hermes, inference host), device-token handling, "
            "attachment parsing risks (zip bombs, malformed PDFs, huge images), voice-approval bypass "
            "(replay, ambient speech, TTS echo), prompt injection via documents, and log leakage.\n\n"
            "**Deliverables:** `docs/security/threat-model.md` with a STRIDE table and each mitigation mapped "
            "to the component that owns it.\n\n"
            "**Acceptance:** every mitigation is either implemented later or tracked by a named card; no "
            "mitigation is left as prose only."
        ),
    ),
    (
        "p0-enable",
        "P0",
        "Enable and smoke-test the Hermes API server",
        "devops",
        ["p0-api"],
        (
            "**Objective:** turn the documented contract into a running, authenticated endpoint.\n\n"
            "**Do:** set `gateway.platforms.api_server.enabled` and the `API_SERVER_*` env vars, bind to "
            "`127.0.0.1`, then verify `/v1/capabilities`, one `/v1/runs` turn, and one SSE event stream.\n\n"
            "**Blocking:** enabling requires a gateway restart. Set the config, then block with "
            "`needs_input` asking the user to run `hermes gateway restart`; resume after they confirm.\n\n"
            "**Deliverables:** `docs/ops/api-server.md` with the exact commands and observed output.\n\n"
            "**Acceptance:** capabilities returns the expected feature flags; a run completes and emits tool "
            "events."
        ),
    ),
    # ---------------- P1 ----------------
    (
        "p1-skeleton",
        "P1",
        "Relay skeleton: config, health, logging",
        "backend-dev",
        [],
        (
            "**Objective:** a runnable service shell with no business logic yet.\n\n"
            "**Do:** FastAPI app, typed settings from env (no secrets in code), structured JSON logging with "
            "correlation ids, `/health` and `/ready`, graceful shutdown, ruff config, pytest scaffold, and a "
            "container/systemd stub.\n\n"
            "**Deliverables:** `relay/app.py`, `relay/config.py`, `relay/tests/test_health.py`, "
            "`relay/pyproject.toml`.\n\n"
            "**Acceptance:** `ruff check relay` clean; `pytest relay/tests` green; `/health` returns 200 and "
            "`/ready` reflects dependency state without calling the network."
        ),
    ),
    (
        "p1-hermes",
        "P1",
        "Hermes adapter: runs, SSE, stop",
        "backend-dev",
        ["p0-api", "p0-enable", "p1-skeleton"],
        (
            "**Objective:** drive a Hermes turn and stream its events to a client.\n\n"
            "**Do:** client for `POST /v1/runs` with `session_id` and `X-Hermes-Session-Key`, an SSE consumer "
            "for `tool.started` / `tool.completed` / `message.interim` / `run.completed`, a stop control, and "
            "reconnect handling with last-event-id.\n\n"
            "**Deliverables:** `relay/hermes/client.py`, `relay/hermes/events.py`, tests against a recorded "
            "fixture.\n\n"
            "**Acceptance:** integration test replays a fixture and asserts ordered events; stop cancels a run "
            "within 500 ms; a dropped SSE reconnects without duplicating the terminal event."
        ),
    ),
    (
        "p1-tests",
        "P1",
        "Integration tests for the text relay",
        "qa-engineer",
        ["p1-hermes"],
        (
            "**Objective:** prove the text path without a live agent.\n\n"
            "**Do:** a stub Hermes API serving recorded SSE fixtures, tests for success, cancellation, error, "
            "and malformed-event paths, plus a contract test that fails if the event schema drifts.\n\n"
            "**Deliverables:** `relay/tests/test_runs.py`, `relay/tests/fixtures/*.jsonl`.\n\n"
            "**Acceptance:** every documented event type has a test; CI runs them offline; no fixture exceeds "
            "2 MB and none contains real user text."
        ),
    ),
    # ---------------- P2 ----------------
    (
        "p2-speech",
        "P2",
        "Speech I/O: streaming STT and TTS adapters",
        "backend-dev",
        ["p1-skeleton", "p0-models"],
        (
            "**Objective:** local speech in and out, fast enough for a call.\n\n"
            "**Do:** streaming STT adapter (sherpa-onnx primary, whisper fallback), partial hypotheses, "
            "sentence-level TTS adapter (Kokoro or qwen3-tts), audio format conversion to 48 kHz Opus, and "
            "backpressure limits.\n\n"
            "**Deliverables:** `relay/speech/stt.py`, `relay/speech/tts.py`, benchmarks in "
            "`docs/research/speech-latency.md`.\n\n"
            "**Acceptance:** final transcript under 300 ms after end of speech; first TTS audio under 250 ms; "
            "no cloud calls; adapters degrade to a clear error when a model is missing."
        ),
    ),
    (
        "p2-brain",
        "P2",
        "Voice brain: LFM tool router with four tools",
        "backend-dev",
        ["p0-models", "p2-speech"],
        (
            "**Objective:** the conversational layer that decides when to delegate.\n\n"
            "**Do:** prompt + tool schema for `delegate_to_hermes`, `answer_direct`, `attach_file`, `end_call`; "
            "direct-answer policy for chit-chat, time, and arithmetic; deferred tool loading if the model "
            "supports it; streaming generation.\n\n"
            "**Deliverables:** `relay/brain/router.py`, `relay/brain/prompts.py`, `relay/brain/tools.json`.\n\n"
            "**Acceptance:** on 20 scripted utterances, tool selection is correct at least 90% of the time; "
            "small talk answers without a Hermes call; a delegation returns immediately."
        ),
    ),
    (
        "p2-barge",
        "P2",
        "Turn detection, barge-in, and interruption",
        "backend-dev",
        ["p2-speech"],
        (
            "**Objective:** make interruption feel instant and safe.\n\n"
            "**Do:** VAD plus semantic end-of-turn, playback flush on speech, stop the active Hermes run, mark "
            "the turn interrupted, and tell the agent it was cut off.\n\n"
            "**Deliverables:** `relay/transport/turns.py`, `relay/tests/test_barge_in.py`.\n\n"
            "**Acceptance:** speech during playback cuts audio within 300 ms; the next turn carries an "
            "interrupted marker; a false trigger does not kill the call."
        ),
    ),
    (
        "p2-bench",
        "P2",
        "Latency benchmark harness",
        "qa-engineer",
        ["p2-speech", "p2-brain"],
        (
            "**Objective:** replace estimates with measurements.\n\n"
            "**Do:** a harness that replays 20 scripted utterances through the pipeline and reports p50/p95 per "
            "stage and end-to-end, plus VRAM and CPU during the run.\n\n"
            "**Deliverables:** `relay/tests/bench_latency.py`, `docs/research/latency-report.md`.\n\n"
            "**Acceptance:** p50 perceived response under 1.2 s; the report states the GPU contention "
            "conditions; reruns are comparable."
        ),
    ),
    # ---------------- P3 ----------------
    (
        "p3-tools",
        "P3",
        "Delegation lifecycle and async results",
        "backend-dev",
        ["p2-brain", "p1-hermes"],
        (
            "**Objective:** make delegation reliable and non-blocking.\n\n"
            "**Do:** enqueue a Hermes run from a tool call, return immediately with an acknowledgement, track "
            "the run id, deliver the final answer to the voice layer, and deduplicate retries with idempotency "
            "keys.\n\n"
            "**Deliverables:** `relay/brain/delegation.py`, `relay/tests/test_delegation.py`.\n\n"
            "**Acceptance:** the tool returns in under 100 ms; the result reaches the caller exactly once; a "
            "failed run produces a spoken failure, not silence."
        ),
    ),
    (
        "p3-narrate",
        "P3",
        "Tool-activity narration",
        "backend-dev",
        ["p3-tools"],
        (
            "**Objective:** let the user hear what the agent is doing without reading logs.\n\n"
            "**Do:** map `tool.started` to a short phrase, throttle to one phrase per four seconds, never read "
            "raw output, and suppress noise from fast tool chains.\n\n"
            "**Deliverables:** `relay/brain/narration.py`, tests with synthetic event streams.\n\n"
            "**Acceptance:** narration starts within 1 s of a tool start; no more than one phrase per 4 s; "
            "secrets never appear (redaction test)."
        ),
    ),
    (
        "p3-approvals",
        "P3",
        "Spoken approval gate for destructive actions",
        "backend-dev",
        ["p3-tools"],
        (
            "**Objective:** approve dangerous work by voice without losing safety.\n\n"
            "**Do:** read the pending command aloud, listen for yes/no, deny on timeout or low confidence, "
            "auto-approve a read-only allowlist, and post the decision with the exact `prompt_id` and "
            "`execution_generation`.\n\n"
            "**Deliverables:** `relay/brain/approvals.py`, `relay/tests/test_approvals.py`.\n\n"
            "**Acceptance:** a destructive command is denied on silence; a stale control id is rejected and "
            "re-asked; the spoken echo matches the command shown to the agent."
        ),
    ),
    (
        "p3-tests",
        "P3",
        "Integration tests: approvals, stop, clarify",
        "qa-engineer",
        ["p3-narrate", "p3-approvals"],
        (
            "**Objective:** cover the control paths that are easy to get wrong.\n\n"
            "**Do:** fixture-driven tests for approval accept/deny/timeout, barge-in stop, clarify question and "
            "answer, and run failure during narration.\n\n"
            "**Deliverables:** `relay/tests/test_controls.py`.\n\n"
            "**Acceptance:** every path asserted; tests run offline; failures include the event trace."
        ),
    ),
    # ---------------- P4 ----------------
    (
        "p4-inbox",
        "P4",
        "Attachment inbox with validation",
        "backend-dev",
        ["p1-skeleton"],
        (
            "**Objective:** accept files safely and hand them to the agent.\n\n"
            "**Do:** multipart upload, MIME sniff, extension allowlist, 50 MB cap, EXIF strip, storage under "
            "`INBOX_DIR` outside the repo, and path injection into the delegation payload.\n\n"
            "**Deliverables:** `relay/storage/inbox.py`, `relay/api/attach.py`, tests.\n\n"
            "**Acceptance:** valid image/PDF/audio accepted; a renamed `.exe` rejected; a 60 MB file rejected "
            "with a clear error; no file is ever written inside the repo."
        ),
    ),
    (
        "p4-outbox",
        "P4",
        "Artifact outbox and authenticated file serving",
        "backend-dev",
        ["p4-inbox"],
        (
            "**Objective:** get generated files back to the phone.\n\n"
            "**Do:** collect deliverables the agent writes to `OUTBOX_DIR`, expose an authenticated download "
            "endpoint, and expire old artifacts.\n\n"
            "**Deliverables:** `relay/storage/outbox.py`, `relay/api/files.py`.\n\n"
            "**Acceptance:** an artifact round-trips to the client; a request without a device token is "
            "rejected; expired files 404."
        ),
    ),
    (
        "p4-tests",
        "P4",
        "End-to-end attachment tests",
        "qa-engineer",
        ["p4-inbox", "p4-outbox", "p3-tools"],
        (
            "**Objective:** prove the attachment story end to end.\n\n"
            "**Do:** synthetic fixtures (a generated PNG, a small PDF, a short WAV) driven through upload → "
            "delegation → artifact download; include a malformed PDF and an oversized image.\n\n"
            "**Deliverables:** `relay/tests/test_attachments.py`, fixtures under `relay/tests/fixtures/`.\n\n"
            "**Acceptance:** image edit and document analysis paths pass; failure cases produce spoken errors; "
            "fixtures total under 2 MB."
        ),
    ),
    # ---------------- P5 ----------------
    (
        "p5-scaffold",
        "P5",
        "Android app scaffold and RTVI client",
        "frontend-dev",
        ["p0-android", "p1-skeleton"],
        (
            "**Objective:** a buildable app that connects to the relay.\n\n"
            "**Do:** Kotlin/Compose project, RTVI/WebRTC client wrapper, settings screen for relay URL and "
            "device token, encrypted token storage, and connection state handling.\n\n"
            "**Deliverables:** `android/app/`, `android/rtvi/`, Gradle files.\n\n"
            "**Acceptance:** `./gradlew assembleDebug` succeeds; the app connects to a stub relay and shows "
            "connected/disconnected states."
        ),
    ),
    (
        "p5-call",
        "P5",
        "Call UI and foreground microphone service",
        "frontend-dev",
        ["p5-scaffold", "p2-barge"],
        (
            "**Objective:** a usable call screen that survives screen-off.\n\n"
            "**Do:** call state machine, mute/end controls, audio visualiser, foreground service with "
            "`microphone` type, audio focus, Bluetooth routing, and battery-optimisation prompt.\n\n"
            "**Deliverables:** `android/app/call/`, manifest entries, instrumentation test.\n\n"
            "**Acceptance:** 10-minute call with the screen off; no audio drop on headset switch; ending the "
            "call releases the microphone."
        ),
    ),
    (
        "p5-kws",
        "P5",
        "On-device wake word",
        "frontend-dev",
        ["p5-scaffold"],
        (
            "**Objective:** start a session hands-free without cloud hotword services.\n\n"
            "**Do:** sherpa-onnx keyword spotting with phrase `hey hermes`, tuned sensitivity, false-trigger "
            "logging, and a push-to-talk fallback.\n\n"
            "**Deliverables:** `android/wakeword/`, model asset under `android/app/src/main/assets/` (small, "
            "checked by size gate).\n\n"
            "**Acceptance:** detection rate above 90% at one metre in a quiet room; fewer than one false "
            "trigger per hour; battery impact measured over one hour."
        ),
    ),
    (
        "p5-attach",
        "P5",
        "Share-sheet, attachment picker, artifact viewer",
        "frontend-dev",
        ["p5-scaffold", "p4-outbox"],
        (
            "**Objective:** send and receive files from the phone.\n\n"
            "**Do:** `ACTION_SEND` target, in-call attachment picker with upload progress, artifact list and "
            "download/open, and device-token auth on every request.\n\n"
            "**Deliverables:** `android/attachments/`.\n\n"
            "**Acceptance:** a file shared from another app reaches the relay; an artifact opens from the call "
            "screen; uploads survive a brief network drop."
        ),
    ),
    (
        "p5-e2e",
        "P5",
        "Android end-to-end and instrumentation tests",
        "qa-engineer",
        ["p5-call", "p5-kws", "p5-attach"],
        (
            "**Objective:** regression coverage for the client.\n\n"
            "**Do:** instrumentation tests for call state, permission denial, token expiry, and upload "
            "retry; an emulator flow that connects to a stub relay.\n\n"
            "**Deliverables:** `android/app/src/androidTest/`.\n\n"
            "**Acceptance:** tests pass in CI on an emulator; a denied microphone permission shows a clear "
            "state rather than a crash."
        ),
    ),
    # ---------------- P6 ----------------
    (
        "p6-hygiene",
        "P6",
        "Secrets and PII gate for a public repo",
        "security-auditor",
        [],
        (
            "**Objective:** make it impossible to leak personal data by accident.\n\n"
            "**Do:** verify gitleaks in CI and pre-commit, the hygiene script (emails, `/home/*`, LAN IPs, "
            "oversized files), and a documented policy. Plant a fake secret and a fake email in a branch to "
            "prove the gate fails.\n\n"
            "**Deliverables:** `scripts/check_repo_hygiene.sh`, `.github/workflows/ci.yml`, "
            "`.pre-commit-config.yaml`, `docs/PRIVACY.md`.\n\n"
            "**Acceptance:** a planted secret fails CI; a clean tree passes; the policy names every rule the "
            "script enforces."
        ),
    ),
    (
        "p6-deploy",
        "P6",
        "Deploy draft: compose, systemd, TLS",
        "devops",
        ["p1-skeleton", "p2-speech"],
        (
            "**Objective:** a reproducible deployment for the relay.\n\n"
            "**Do:** compose file with env-file only (no inline secrets), systemd unit alternative, reverse "
            "proxy with internal TLS, port table, health checks, and a rollback procedure.\n\n"
            "**Deliverables:** `deploy/compose.yaml`, `deploy/hermes-talk.service`, `deploy/Caddyfile`, "
            "`docs/DEPLOYMENT.md`.\n\n"
            "**Acceptance:** deploy from a clean host using only the repo and an `.env`; health check green; "
            "rollback documented and rehearsed once."
        ),
    ),
    (
        "p6-docs-arch",
        "P6",
        "Architecture guide and diagrams",
        "technical-writer",
        ["p1-hermes", "p2-brain", "p3-tools"],
        (
            "**Objective:** documentation that matches the shipped code.\n\n"
            "**Do:** component diagram, data flow, trust boundaries, and the relay↔Hermes contract with real "
            "examples. Generic paths and TEST-NET addresses only.\n\n"
            "**Deliverables:** `docs/ARCHITECTURE.md`.\n\n"
            "**Acceptance:** every diagram matches the code; no personal paths or LAN IPs; a new contributor "
            "can locate each component."
        ),
    ),
    (
        "p6-docs-api",
        "P6",
        "Relay API reference",
        "technical-writer",
        ["p1-hermes", "p3-tools"],
        (
            "**Objective:** a complete reference for the relay's HTTP/WS surface.\n\n"
            "**Do:** endpoints, request/response schemas, event names, error codes, auth, and rate limits; "
            "generated examples must be executable.\n\n"
            "**Deliverables:** `docs/API.md`.\n\n"
            "**Acceptance:** every endpoint has an example that was run; error codes match the implementation."
        ),
    ),
    (
        "p6-docs-ops",
        "P6",
        "Operator runbook",
        "technical-writer",
        ["p6-deploy"],
        (
            "**Objective:** someone else can run the system.\n\n"
            "**Do:** start/stop, log locations, token rotation, model warm-up, common failures and fixes, "
            "backup of inbox/outbox, and the no-restart rule for the gateway.\n\n"
            "**Deliverables:** `docs/RUNBOOK.md`.\n\n"
            "**Acceptance:** an operator follows it start-to-finish without asking a question."
        ),
    ),
    (
        "p6-review",
        "P6",
        "Code review gate: relay and adapter",
        "code-reviewer",
        ["p1-hermes", "p3-approvals", "p4-inbox"],
        (
            "**Objective:** independent review before release.\n\n"
            "**Do:** review the relay, Hermes adapter, approval gate, and attachment handling for correctness, "
            "secret handling, error paths, and test coverage; file findings as cards or fix in place with tests.\n\n"
            "**Deliverables:** review notes in the card thread, linked follow-up cards.\n\n"
            "**Acceptance:** no open high-severity finding; every finding has a test or a tracked card."
        ),
    ),
    (
        "p6-release",
        "P6",
        "v0.1 release checklist",
        "planner",
        [
            "p5-e2e",
            "p6-review",
            "p6-docs-arch",
            "p6-docs-api",
            "p6-docs-ops",
            "p6-hygiene",
        ],
        (
            "**Objective:** ship a defensible first release.\n\n"
            "**Do:** versioning, changelog, licence check, CI green, public-repo audit (no PII, no secrets, no "
            "large files), install instructions tested from scratch, and a tagged release.\n\n"
            "**Deliverables:** `CHANGELOG.md`, `docs/RELEASE.md`, tag `v0.1.0`.\n\n"
            "**Acceptance:** a clean clone builds and runs; CI green on the tag; hygiene script clean."
        ),
    ),
]


def run(args: list[str]) -> str:
    proc = subprocess.run(args, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit(f"command failed: {' '.join(args)}")
    return proc.stdout


def create(
    title: str,
    body: str,
    assignee: str | None,
    key: str,
    triage: bool,
    parents: list[str],
) -> str:
    args = [
        "hermes",
        "kanban",
        "--board",
        BOARD,
        "create",
        title,
        "--body",
        body,
        "--workspace",
        f"dir:{WORKDIR}",
        "--idempotency-key",
        f"ht-{key}",
        "--json",
    ]
    if assignee:
        args += ["--assignee", assignee]
    if triage:
        args += ["--triage"]
    for p in parents:
        args += ["--parent", p]
    out = run(args)
    return json.loads(out.strip())["id"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ids: dict[str, str] = {}

    for key, title, body in EPICS:
        if args.dry_run:
            print(f"{key:12} EPIC  {title}")
            ids[key] = key
            continue
        ids[key] = create(
            title, body + "\n\n" + CONSTRAINTS, None, key, triage=True, parents=[]
        )
        print(f"{key:12} -> {ids[key]}  {title}")

    # Topological creation: parents must exist before children.
    pending = list(TASKS)
    while pending:
        progressed = False
        for card in list(pending):
            key, phase, title, assignee, parents, body = card
            if any(p not in ids for p in parents):
                continue
            if args.dry_run:
                print(
                    f"{key:12} {phase:3} {assignee:16} parents={parents or '-'}  {title}"
                )
                ids[key] = key
            else:
                ids[key] = create(
                    title,
                    body + "\n\n" + CONSTRAINTS,
                    assignee,
                    key,
                    triage=False,
                    parents=[ids[p] for p in parents],
                )
                print(f"{key:12} -> {ids[key]}  {title}")
            pending.remove(card)
            progressed = True
        if not progressed:
            raise SystemExit(
                f"dependency cycle or unknown parent: {[c[0] for c in pending]}"
            )

    if args.dry_run:
        print(f"\n{len(EPICS)} epics, {len(TASKS)} tasks")
        return

    print(f"\nseeded {len(EPICS)} epics, {len(TASKS)} tasks on board '{BOARD}'")
    print(run(["hermes", "kanban", "--board", BOARD, "stats"]).strip())


if __name__ == "__main__":
    main()
