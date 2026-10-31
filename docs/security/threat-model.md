# Threat model — Hermes Talk

> **Status:** reviewed pre-implementation (P0), 2026-10-31. Scope: the architecture described in
> `docs/ARCHITECTURE.md` before any product code exists. This document is a design gate: every
> mitigation below is owned by a named component and tracked by a named kanban card. It is re-reviewed
> at P6 (`Code review gate: relay and adapter`, `v0.1 release checklist`).

## 1. How to read this

- Threat IDs (`T-01` … `T-31`) are stable references used by cards, tests, and review notes.
- Every row in the STRIDE register carries an **owner component** and one or more **tracking cards**
  (board `hermes-talk`). A mitigation with no card is a defect in this document, not a to-do.
- Card IDs are the durable identifiers; titles are given in the ledger (§8) so a reader can find them
  without the board.
- Controls marked *by design* are properties of a chosen technology (e.g. WebRTC DTLS-SRTP); they are
  still mapped to the card that must verify them, because an unverified property is an assumption.

## 2. System and trust boundaries

```
        ┌──────────────────────────────────────────────┐
        │ TB1  Android client (phone)                  │
        │  mic · speaker · Keystore · wake word        │
        └───────────────┬──────────────────────────────┘
                        │ WebRTC media (DTLS-SRTP)
                        │ HTTPS/WSS control (bearer token)
                        │ LAN, or tailnet for off-LAN use
        ┌───────────────▼──────────────────────────────┐
        │ TB2  voice-relay (relay host)                │
        │  transport · VAD/STT/TTS · voice brain       │
        │  Hermes adapter · inbox/outbox · auth · logs │
        └───┬───────────────────────────┬──────────────┘
            │ TB3 HTTP 127.0.0.1        │ TB4 HTTP 127.0.0.1
            │ bearer API key            │ process-level trust
        ┌───▼───────────────┐   ┌───────▼──────────────┐
        │ Hermes API server │   │ inference host       │
        │ 127.0.0.1:8642    │   │ speech + agent model │
        └───┬───────────────┘   └──────────────────────┘
            │ TB5 tool authority: shell, filesystem, network
        ┌───▼──────────────────────────────────────────┐
        │ Host OS and filesystem ($DATA_DIR, repo)     │
        └──────────────────────────────────────────────┘
```

| Boundary | What crosses it | Trust posture |
|---|---|---|
| TB1 phone ↔ relay | audio, transcripts, files, control messages | network is untrusted; device is authenticated, not trusted |
| TB2 relay internals | audio frames, model output, tool events | trusted process, untrusted inputs |
| TB3 relay ↔ Hermes API | prompts, tool events, approvals, session ids | loopback only, bearer key, session ownership enforced |
| TB4 relay ↔ inference | prompts, audio, embeddings | loopback only, no transport auth (host process trust) |
| TB5 Hermes ↔ tools | shell commands, file writes, network calls | highest authority in the system; approval-gated |
| TB6 repo ↔ public internet | source, docs, fixtures | public; must contain no PII, secrets, or host paths |
| TB7 environment ↔ mic/speaker | ambient speech, TTS playback, echo | physically untrusted; cannot be authenticated |
| TB8 logs ↔ reader | transcripts, tool output, identifiers | local only, but long-lived and easy to leak |

## 3. Assets

| Asset | Where it lives | Why it matters |
|---|---|---|
| Tool authority (shell, file writes, network) | Hermes agent on the relay host | highest impact: a wrong approval can delete data |
| Device bearer tokens | phone Keystore; relay stores a hash | possession grants full control of the relay |
| `HERMES_API_KEY` | relay `.env` / process env | grants agent turns and tool execution |
| Session identity (`session_id`, `X-Hermes-Session-Key`) | relay config, Hermes DB | wrong binding mixes conversations and memory |
| Audio and transcripts | relay memory; Hermes session DB | personal content, potentially sensitive |
| Attachments and artifacts | `$INBOX_DIR`, `$OUTBOX_DIR` | personal documents and photos |
| Logs and audit trail | `$DATA_DIR` | proof of what happened; leak surface |
| Model weights and speech assets | inference host, model cache | integrity of behaviour; licence compliance |

## 4. Adversaries and assumptions

| Actor | Capability assumed |
|---|---|
| A1 LAN peer without a token | can reach the relay port, open WebRTC offers, upload nothing |
| A2 LAN peer with a stolen token | full API and media access until revocation |
| A3 document author | controls bytes of a PDF/image/zip the user sends; no account |
| A4 ambient environment | TV, bystanders, smart speaker saying "yes" at the wrong time |
| A5 borrowed or compromised phone | can read what the app can read; can press buttons |
| A6 unprivileged local process on the relay host | can reach loopback ports and read world-readable files |
| A7 public-repo scraper / CI log reader | sees everything committed or printed in CI |
| A8 operator error | misconfigured env, wrong cert, forgotten token |

Assumptions:

- Single user, single household; no multi-tenant isolation in v1.
- The relay host OS and its full-disk encryption are trusted. Physical access to the host is out of scope.
- The LAN is not assumed hostile at the link layer, but it is not trusted for authentication: every
  request carries a token, every media stream is DTLS-SRTP.
- Hermes' own tool policy, sandboxing, and workspace-root enforcement are trusted; the relay only adds
  the spoken approval gate on top.
- The phone's Android Keystore is trusted to protect the token at rest.
- No component calls the internet at runtime (ADR 0001). Model downloads happen once, at install.

Out of scope for v1: multi-user authorization, voice biometrics, internet-facing exposure, model
supply-chain verification beyond pinned filenames and checksums, Android OS compromise, side-channel
attacks on the host.

## 5. STRIDE threat register

Every mitigation is mapped to the component that owns it and to the card that will implement or verify
it. `by design` controls are verified by the listed card, not assumed.

| ID | STRIDE | Threat | Impact | Mitigation | Owner component | Tracking card(s) |
|---|---|---|---|---|---|---|
| T-01 | Spoofing | Unauthenticated LAN peer connects to the relay and sends audio or files | eavesdrop-free but unauthorized control; wasted GPU | Bearer token required on every HTTP/WS request before media is accepted; WebRTC media authenticated by DTLS-SRTP fingerprint | relay auth (`relay/auth`) + Android RTVI client | `t_e6939201`, `t_6fa8262c`, `t_5cafa862` |
| T-02 | Spoofing | A leaked device token is reused from another device; revocation needs a restart or an env edit | attacker acts as the user until someone notices | Per-device tokens, SHA-256 hash at rest, live revoke/rotate without restart, constant-time compare, audit line per failure | relay auth | `t_e6939201`, `t_503d6772` |
| T-03 | Spoofing | Relay impersonated on the LAN (MITM, self-signed cert, client that trusts everything) | audio and tokens intercepted or altered | TLS terminator with internal CA; SPKI pinning in the app; fail closed on mismatch; no trust-all manager | deployment (reverse proxy) + Android client | `t_9856ed4b`, `t_5cafa862` |
| T-04 | Spoofing | A local process on the relay host calls Hermes or inference as if it were the relay | unauthorized agent turns, tool execution | Loopback binds only; bearer key from env, never in the repo; Hermes refuses keys shorter than 16 chars; no published container ports for loopback services | deployment + Hermes API server config | `t_432af79b`, `t_5cafa862`, `t_ff840cd1` |
| T-05 | Spoofing | Relay binds or replays a session owned by another surface, mixing memory scopes | wrong context, cross-surface data bleed | Session ownership enforced by Hermes (`409 permission_denied`); one declared session key per conversation; adapter test asserts the refusal | Hermes adapter | `t_a058fbb2`, `t_1fd21017` |
| T-06 | Spoofing | Wake-word false trigger opens a microphone session and captures ambient audio | privacy surprise; wasted GPU; stray commands | On-device KWS sensitivity tuning, visible listening indicator, push-to-talk fallback, idle-session timeout | Android client | `t_bf945daf`, `t_a7d5bea2`, `t_46b69543` |
| T-07 | Tampering | Client-supplied filename traverses out of the inbox, overwrites files, or makes the agent read an arbitrary host path | data loss; unintended disclosure | Basename only, server-generated UUID filenames, canonical path assertion under `$INBOX_DIR` before delegation, never trust extension | relay storage | `t_7ae6943b` |
| T-08 | Tampering | Attachment changed between validation and agent read (TOCTOU), or a hash mismatch goes unnoticed | agent analyzes different bytes than were validated | Content hash recorded at ingest, immutable per-turn inbox path, hash passed with the path, mismatch refuses the turn | relay storage + Hermes adapter | `t_7ae6943b`, `t_0789b339` |
| T-09 | Tampering | Decompression bomb, huge image, malformed or encrypted PDF exhausts memory or hangs the parser mid-call | relay OOM or stall; call dies | Wire-size cap plus expansion caps (entries, uncompressed bytes, ratio, pages, pixels, decode time); parser in an isolated subprocess with timeout and rlimit; fail closed with a spoken error | relay storage | `t_0789b339`, `t_7ae6943b` |
| T-10 | Tampering | Agent writes an artifact outside the outbox, or a symlink escapes it during download | stray files; disclosure of unrelated host files | Outbox root confinement, symlink rejection, authenticated download, expiry, no traversal in path resolution | relay storage | `t_6d96d0e5` |
| T-11 | Tampering | A document's text alters agent behaviour (prompt injection) | wrong tool choice; destructive action proposed | Provenance envelope around untrusted spans, path-not-text handoff, truncation markers, spoken-only approval | relay brain + Hermes adapter | `t_715b5ee3` |
| T-12 | Tampering | Speech or model asset is stale, swapped, or missing; adapter silently falls back | wrong transcript or wrong voice; hidden cloud dependency | Pinned model identifiers, checksum recorded at install, explicit error when missing, no cloud fallback | speech adapters + ops runbook | `t_a6cdeb50`, `t_503d6772` |
| T-13 | Repudiation | No durable record of who approved which destructive action, or which file was analyzed | disputes cannot be settled; audits impossible | Append-only audit log with `prompt_id`, `execution_generation`, decision, transcript hash, attachment hash, timestamp | relay logging | `t_10de434c`, `t_b6ecaff6` |
| T-14 | Repudiation | A denial, timeout, or failed run looks identical to success (silence) | user believes work happened when it did not | Spoken failure on deny/timeout/error; run status recorded; audit entry for every terminal state | relay brain + delegation | `t_a4068f82`, `t_86350c79` |
| T-15 | Repudiation | Logs rotate away or are overwritten, losing the trail | approval history disappears | Append-only audit file with size/age rotation and retention; audit entries never rewritten | relay logging | `t_10de434c` |
| T-16 | Information disclosure | Transcripts, attachment text, or raw audio leak through logs, tracebacks, CI output, or the repo | personal content on disk or in public | Redaction at the logging boundary, transcript truncation, no raw audio unless explicitly enabled, synthetic fixtures only, hygiene gate | relay logging + CI | `t_10de434c`, `t_ff840cd1`, `t_884ba442` |
| T-17 | Information disclosure | Device token, `HERMES_API_KEY`, or session key appears in logs, URLs, or the repo | anyone reading logs or the repo can act as the relay | Header-only tokens (never query strings), redaction of auth material, gitleaks and hygiene script, `.env.example` contains placeholders only | relay auth + logging + CI | `t_e6939201`, `t_10de434c`, `t_ff840cd1` |
| T-18 | Information disclosure | Relay serves plaintext on the LAN, exposing audio and bearer tokens | passive LAN observer reads everything | TLS required by the deployment; plaintext allowed only for `127.0.0.1` development; reverse proxy terminates internal TLS | deployment | `t_5cafa862`, `t_9856ed4b` |
| T-19 | Information disclosure | Hermes API or inference host reachable from the LAN | other hosts can submit turns or query the model | Bind `127.0.0.1` only; no port publishing; documented port table | deployment | `t_5cafa862`, `t_432af79b` |
| T-20 | Information disclosure | A component phones home (telemetry, cloud STT/TTS fallback, update check) | audio or text leaves the LAN; violates the core promise | Local-only invariant, offline CI, explicit error instead of cloud fallback, release checklist audit | speech/brain adapters + release gate | `t_a6cdeb50`, `t_c9177c13`, `t_787259e7` |
| T-21 | Information disclosure | Artifact download endpoint serves an unauthenticated caller or a file outside the outbox | personal files exposed to the LAN | Device token required, root confinement, 404 for expired or unknown files | relay storage | `t_6d96d0e5` |
| T-22 | Information disclosure | Token readable on a borrowed phone (plain prefs, backup, logcat) | full relay control from another device | Keystore-backed encrypted storage, excluded from backups, never logged; revoke on loss | Android client + relay auth | `t_6fa8262c`, `t_e6939201` |
| T-23 | Denial of service | Unbounded sessions, runs, or uploads exhaust memory, GPU, or the event loop | call drops for the legitimate user | Caps on concurrent sessions/runs/queues, per-device rate limits, bounded queues with `429`/`503`, idle timeouts | relay | `t_46b69543`, `t_7ae6943b` |
| T-24 | Denial of service | Oversized audio stream or a stalled SSE stream ties up the pipeline | relay stops answering | Frame-size caps, backpressure, stream idle timeout, bounded reconnects, stop the run when the session dies | relay speech + Hermes adapter | `t_a6cdeb50`, `t_a058fbb2`, `t_46b69543` |
| T-25 | Denial of service | Barge-in storm (noisy room, feedback loop) cancels runs repeatedly | work never completes | Minimum speech duration, debounce, cooldown, one stop per utterance | relay transport | `t_64d6ecc5`, `t_46b69543` |
| T-26 | Elevation of privilege | Replay of an earlier "yes" (recorded audio or a previous turn) approves a new destructive action | unintended deletion or write | Decision bound to the exact `prompt_id` and `execution_generation`, single-use; `409 stale_generation` / `409 approval_not_pending` means re-ask once then deny | relay brain + approvals | `t_b6ecaff6`, `t_86350c79` |
| T-27 | Elevation of privilege | TTS self-echo: the relay transcribes its own "yes" or question and approves itself | destructive action without a human | Half-duplex approval window; accept only utterances whose onset follows prompt playback; never transcribe playback frames | relay approvals + transport | `t_b6ecaff6`, `t_64d6ecc5` |
| T-28 | Elevation of privilege | Ambient or bystander speech approves ("yes" from a TV or another person) | destructive action without the user's intent | Explicit confirmation phrase naming the action, ASR confidence floor, deny on ambiguity or overlap, one pending prompt per run | relay approvals | `t_b6ecaff6`, `t_86350c79` |
| T-29 | Elevation of privilege | Prompt injection escalates authority: document text triggers a destructive tool, or a tool's raw output is read as an instruction | destructive action attributed to the document | Spoken gate is the only approver; provenance envelope; four-tool brain allowlist; narration maps phrases, never raw output; tool output truncated with a marker | relay brain + approvals | `t_715b5ee3`, `t_86350c79`, `t_f526f882` |
| T-30 | Elevation of privilege | A destructive shell command or file write executes without a spoken confirmation, or escapes the workspace root | data loss outside the intended directory | Approval gate for destructive tools with deny-on-silence; workspace-root policy; outbox confinement | relay approvals + Hermes tool policy | `t_86350c79`, `t_6d96d0e5` |
| T-31 | Elevation of privilege | Attachment is executed (renamed `.exe`, macro, script) or its parser has a code path that runs code | unexpected execution, parser CVE | MIME sniff against an allowlist, never execute, agent reads bytes/path only, parsers kept minimal and isolated | relay storage | `t_7ae6943b`, `t_0789b339` |

## 6. Area detail

### 6.1 Trust boundaries and transport

TB1 is the only boundary a non-privileged attacker can reach. Controls:

- Every HTTP/WS request carries `Authorization: Bearer <device token>`; the token is never accepted
  from a query string or the WebRTC data channel. (T-01, T-17)
- Media is DTLS-SRTP by virtue of WebRTC; the app must not disable it or fall back to plain RTP. (T-01)
- TLS terminates at a reverse proxy with an internal CA; the app pins the relay's SPKI and fails
  closed. Plaintext HTTP is allowed only for `127.0.0.1` development via `adb reverse`. (T-03, T-18)
- TB3 uses loopback plus a bearer key; the key is validated by the Hermes API server (minimum length,
  `401` on mismatch). The relay never reuses a session id across surfaces; a foreign session is a
  `409 permission_denied` that the adapter surfaces as a spoken error. (T-04, T-05)
- TB4 is process-level trust: inference binds to `127.0.0.1`, has no transport auth, and is never
  published outside the host. (T-19)
- TB7 cannot be authenticated. All acoustic assumptions are therefore explicit in §6.4.

### 6.2 Device-token handling

Current state: `.env.example` lists `DEVICE_TOKENS` as comma-separated plaintext, one per device. That
is workable for a bootstrap but weak for a security control:

- any process that can read the environment or `.env` sees every token;
- revocation is an edit plus a restart, and a restart needs the user's explicit go;
- there is no per-device identity in logs, so "which phone did that?" is unanswerable.

Required design (`t_e6939201`):

1. Issue: 32 random bytes, base64url, `ht_` prefix, generated by a loopback-only admin command.
2. Store: SHA-256 hash, device label, issued-at, and a token id prefix in a `0600` state file under
   `$DATA_DIR`. Plaintext is shown once.
3. Verify: constant-time comparison, header only, `401` before any media or work is accepted.
4. Revoke/rotate: effective on the next request without a process restart; rotation is atomic.
5. Audit: one line per issue, revoke, and auth failure (label and prefix only).
6. Client: token stored in Android Keystore, excluded from backups, never logged (`t_6fa8262c`).

Residual: a bearer token is possession-based; there is no per-request proof-of-possession in v1. A
stolen token works from any host until revoked. Accepted for a single-user LAN.

### 6.3 Attachment parsing

The relay accepts bytes from a phone, validates them, stores them outside the repo, and hands the agent
a path. That path is the dangerous part: Hermes reads whatever path it is given.

Threat surface: zip bombs, PDF object bombs and page floods, huge decoded images, truncated or
encrypted PDFs, polyglots and renamed executables, EXIF/GPS metadata, symlinks, traversal, parser CVEs,
and TOCTOU between validation and read.

Controls:

| Control | Value / behaviour | Card |
|---|---|---|
| Wire-size cap | `MAX_ATTACHMENT_MB` (default 50) | `t_7ae6943b` |
| Content sniff | magic bytes, not extension; allowlist image/PDF/audio/plain text | `t_7ae6943b` |
| Archive caps | max entries, max total uncompressed bytes, max compression ratio; abort, never partially extract | `t_0789b339` |
| PDF caps | page count, extracted-text bytes, wall-clock timeout, isolated parser, reject encrypted/corrupt | `t_0789b339` |
| Image caps | decoded pixel cap, decode-time cap, downscale before use, single decode | `t_0789b339` |
| Path safety | server-generated UUID filename, basename only, canonical path asserted under `$INBOX_DIR` | `t_7ae6943b` |
| Integrity | SHA-256 recorded at ingest and passed with the path; mismatch refuses the turn | `t_0789b339` |
| Metadata | EXIF stripped before storage; GPS never reaches the agent unless asked | `t_7ae6943b` |
| Execution | files are never executed; the agent reads bytes or a path | `t_7ae6943b` |
| Failure | every rejection is a spoken, non-technical message; no traceback to the user | `t_0789b339` |

Residual: a valid file can still be semantically hostile (a 40-page contract with a hidden instruction);
that is handled by §6.5, not by the parser.

### 6.4 Voice approval

The spoken approval is the system's only human-in-the-loop control for destructive tools. It must be
treated as a security mechanism, not a convenience. Four failure classes:

1. **Replay.** A recorded "yes", or the previous turn's "yes", satisfies a new prompt. Control: bind
   the decision to `prompt_id` + `execution_generation`, post exactly once, treat `409` as stale and
   re-ask once before denying (`t_b6ecaff6`).
2. **Self-echo.** The relay hears its own TTS ("Shall I run it?" contains no "yes", but a confirmation
   echo might) and approves itself. Control: half-duplex window; accept only utterances whose VAD onset
   is after playback ended; never transcribe playback frames (`t_b6ecaff6`, `t_64d6ecc5`).
3. **Ambient speech.** A TV, a bystander, or a smart speaker says "yes". Control: explicit confirmation
   phrase naming the action ("yes, run it"), ASR confidence floor, deny on overlapping speakers, one
   pending prompt per run (`t_b6ecaff6`, `t_86350c79`).
4. **Race and silence.** Two prompts pending, a barge-in during the read-out, or no answer at all.
   Control: deny on timeout; cancel the pending prompt on barge-in or stop; the spoken echo must match
   the command shown to the agent; a stale control id is rejected and re-asked (`t_86350c79`,
   `t_951af892`).

Additional rules: approval defaults to deny; read-only allowlisted tools may auto-approve; document
text and tool output can never satisfy an approval; every decision writes an audit entry with the
transcript hash (`t_10de434c`). No voice biometrics in v1 — the phrase plus confidence is the control.

### 6.5 Prompt injection

Untrusted text reaches the agent through four channels: attachment text (PDF body, OCR, filenames),
tool output, narration, and the session history. The voice brain's own output is semi-trusted.

Controls (`t_715b5ee3`):

- Provenance envelope: untrusted spans are wrapped in explicit delimiters with the source name and
  SHA-256; the relay preamble states that content inside is data, never instructions.
- Path-not-text: the agent receives a path plus hash where possible; inlined excerpts are truncated and
  marked as truncated.
- Approval isolation: only the spoken gate approves; a document cannot approve itself.
- Capability isolation: the voice brain keeps its four-tool allowlist; injection cannot invent tools.
- Narration: `tool.started` maps to a fixed phrase; raw output is never read aloud and never becomes
  the next prompt (`t_f526f882`).

Residual: a document can still persuade the model to choose a different *non-destructive* tool (e.g.
read a different file). Blast radius is bounded by the approval gate, the workspace root, and the
four-tool allowlist. Accepted.

### 6.6 Log leakage

Logs are the most likely leak path because they are verbose, long-lived, and easy to copy. Surfaces:
relay application logs, audit log, Hermes session DB, CI output, crash reports, Android logcat.

Controls (`t_10de434c`, `t_ff840cd1`):

- Redaction at the logging boundary: auth headers, API keys, session keys, and device tokens become
  `***`; paths are logged relative to `$DATA_DIR`; transcripts are truncated and marked.
- No raw audio unless `RECORD_AUDIO=1`, and then under `$DATA_DIR/recordings`, never in the repo.
- Separate append-only audit file (`0600`) for auth, tokens, attachments, approvals, and run lifecycle.
- Retention: age and size caps with rotation for logs; inbox/outbox/transcript retention and
  deletion are owned by `t_d87f133b` (with artifact expiry in `t_6d96d0e5`).
- CI logs: synthetic fixtures only, no real transcripts, no real photos (`t_884ba442`, `t_034c4c1e`).
- A redaction test plants a secret and asserts it never reaches the sink, including tracebacks.

## 7. Privacy review

### 7.1 Data inventory

| Data | Location | Lifetime | Leaves the LAN |
|---|---|---|---|
| Microphone audio | relay process memory | call duration; disk only if recording is enabled | no |
| Transcripts | Hermes session DB on the relay host | until session deletion / retention policy | no |
| Attachments | `$INBOX_DIR` (outside the repo) | until retention deletes them | no |
| Artifacts | `$OUTBOX_DIR` | until expiry | no |
| Device token | phone Keystore (plaintext); relay stores hash | until revoke | no |
| `HERMES_API_KEY` | relay env / `.env` | until rotation | no |
| Logs and audit | `$DATA_DIR` | retention window | no |
| Telemetry | none | — | no |

The only optional off-LAN traffic is tailnet coordination if the user reaches the relay from outside the
home network; that service sees connection metadata, not content.

### 7.2 Local-only verification

- ADR 0001 is the invariant. Verification is not a promise: `t_a6cdeb50` and `t_c9177c13` must fail
  loudly when a local model is missing rather than fall back to a cloud service, and `t_884ba442`
  requires CI to run offline. `t_787259e7` audits the release for egress.
- Model downloads are install-time only; runtime paths are `127.0.0.1`.

### 7.3 Repository hygiene

`docs/PRIVACY.md` lists the rules; `scripts/check_repo_hygiene.sh` and CI enforce them (`t_ff840cd1`):
no personal emails, no `/home/...` paths, no private LAN addresses, no secret-looking strings, no file
over 2 MB. Fixtures are synthetic. This document itself uses `127.0.0.1`, `0.0.0.0`, and
`voice.example.lan` only.

### 7.4 Retention and deletion

Retention defaults belong in `.env.example` and the runbook, not in code (`t_d87f133b`). Deletion is
logged to the audit trail (`t_10de434c`): attachment files are unlinked after the retention window,
artifacts expire (`t_6d96d0e5`), transcripts are deleted through the Hermes session API, logs rotate.
The policy is documented in `docs/DEPLOYMENT.md` and the operator runbook (`t_503d6772`).

## 8. Mitigation ledger

Every mitigation above is tracked. New cards were created by this review; existing cards are listed
where their acceptance criteria already cover the control.

### 8.1 New cards created by this review

| Card | Title | Assignee | Covers threats | Gated on |
|---|---|---|---|---|
| `t_e6939201` | Device token lifecycle: hashing, rotation, revocation | backend-dev | T-01, T-02, T-17, T-22 | `t_d1155612` |
| `t_0789b339` | Attachment parsing hardening: bombs, malformed files, parser isolation | backend-dev | T-08, T-09, T-31 | `t_7ae6943b` |
| `t_b6ecaff6` | Approval gate hardening: self-echo, ambient speech, replay | backend-dev | T-13, T-26, T-27, T-28 | `t_86350c79` |
| `t_10de434c` | Log redaction, retention, and approval audit trail | backend-dev | T-13, T-15, T-16, T-17 | `t_d1155612` |
| `t_715b5ee3` | Prompt-injection containment for attachments and tool output | backend-dev | T-11, T-29 | `t_7ae6943b` |
| `t_9856ed4b` | Android TLS trust for the internal relay certificate | frontend-dev | T-03, T-18 | `t_6fa8262c` |
| `t_46b69543` | Relay admission control and rate limits | backend-dev | T-06, T-23, T-24, T-25 | `t_d1155612` |
| `t_d87f133b` | Data retention and deletion: inbox, outbox, transcripts | backend-dev | T-15, T-16 | `t_7ae6943b` |

### 8.2 Existing cards relied on

| Card | Title | Covers threats |
|---|---|---|
| `t_432af79b` | Enable and smoke-test the Hermes API server | T-04, T-19 |
| `t_d1155612` | Relay skeleton: config, health, logging | T-17 (typed settings, no secrets in code) |
| `t_a058fbb2` | Hermes adapter: runs, SSE, stop | T-05, T-24 |
| `t_884ba442` | Integration tests for the text relay | T-16, T-20 |
| `t_a6cdeb50` | Speech I/O: streaming STT and TTS adapters | T-12, T-20, T-24 |
| `t_c9177c13` | Voice brain: LFM tool router with four tools | T-20, T-29 |
| `t_64d6ecc5` | Turn detection, barge-in, and interruption | T-25, T-27 |
| `t_a4068f82` | Delegation lifecycle and async results | T-14 |
| `t_f526f882` | Tool-activity narration | T-29 |
| `t_86350c79` | Spoken approval gate for destructive actions | T-14, T-26, T-28, T-29, T-30 |
| `t_951af892` | Integration tests: approvals, stop, clarify | T-26, T-28 |
| `t_7ae6943b` | Attachment inbox with validation | T-07, T-08, T-09, T-23, T-31 |
| `t_6d96d0e5` | Artifact outbox and authenticated file serving | T-10, T-21, T-30 |
| `t_034c4c1e` | End-to-end attachment tests | T-09, T-16 |
| `t_6fa8262c` | Android app scaffold and RTVI client | T-01, T-22 |
| `t_a7d5bea2` | Call UI and foreground microphone service | T-06 |
| `t_bf945daf` | On-device wake word | T-06 |
| `t_a8a47214` | Share-sheet, attachment picker, artifact viewer | T-22 |
| `t_5c52a637` | Android end-to-end and instrumentation tests | T-01 (DTLS-SRTP verified on device) |
| `t_ff840cd1` | Secrets and PII gate for a public repo | T-04, T-16, T-17 |
| `t_5cafa862` | Deploy draft: compose, systemd, TLS | T-01, T-03, T-04, T-18, T-19 |
| `t_503d6772` | Operator runbook | T-02, T-12 (token rotation, model warm-up) |
| `t_1efd8db5` | Code review gate: relay and adapter | all — independent verification |
| `t_787259e7` | v0.1 release checklist | T-20, all — release gate |
| `t_1fd21017` | Document the Hermes API contract for the relay | T-05 |

## 9. Verification plan

| Control | Proven by |
|---|---|
| Token issue/revoke/rotate, no token in logs | `t_e6939201` tests + `t_10de434c` redaction test |
| Bomb/malformed-file rejection without OOM | `t_0789b339` fixtures + `t_034c4c1e` end-to-end |
| Approval replay/echo/ambient denial | `t_b6ecaff6` + `t_951af892` |
| Injection cannot approve or invent tools | `t_715b5ee3` + `t_951af892` |
| TLS pinning fails closed | `t_9856ed4b` Android tests |
| Caps and rate limits hold under load | `t_46b69543` + `t_5c52a637` soak |
| Retention deletes only expired, idle data | `t_d87f133b` tests |
| No secrets/PII in repo or logs | `t_ff840cd1` + `t_1efd8db5` |
| No runtime egress | `t_884ba442` offline CI + `t_787259e7` release audit |
| Independent review of all of the above | `t_1efd8db5` |

## 10. Residual and accepted risks

| Risk | Why accepted | Owner / review point |
|---|---|---|
| Bearer tokens are possession-based; a stolen token works from any host | single-user LAN; revocation is live; rotation is documented | `t_e6939201`, `t_503d6772` |
| No at-rest encryption of transcripts, inbox, or outbox in v1 | relies on full-disk encryption of the relay host; adding per-file encryption complicates the agent's path-based reads | P6 review (`t_1efd8db5`) |
| Loopback HTTP + shared bearer key between relay and Hermes | any local process that can read the env can impersonate the relay; acceptable on a single-user host | `t_432af79b` |
| ASR cannot identify speakers; approval is phrase + confidence, not voiceprint | voice biometrics are out of scope for v1 and add latency | P6 review |
| A valid document can be semantically hostile without being malformed | approval gate bounds the blast radius; injection controls reduce misreads | `t_715b5ee3` |
| Certificate rotation requires re-pinning the app | documented in `android/README.md`; failure is loud, not silent | `t_9856ed4b` |
| Tailnet coordination metadata leaves the LAN when used off-LAN | metadata only, no content; optional | `docs/PRIVACY.md` |

## 11. Open questions

1. Should v1 add speaker verification (a lightweight embedding) to strengthen the approval gate, or is
   phrase + confidence sufficient for a single-user household?
2. Should transcripts be encrypted at rest independently of full-disk encryption?
3. What are the default retention windows for inbox attachments and artifacts — days, or "until the
   session ends"?
4. Is tailnet exposure in scope for v1, and if so does the relay need a second auth factor for off-LAN
   clients?
5. Do we want per-request proof-of-possession (HMAC nonce) for device tokens, or is bearer-only
   acceptable until multi-device support?

## 12. Sign-off

Reviewed 2026-10-31 against the pre-implementation architecture. No mitigation in this document is
prose-only: each is mapped to a component and a card in §8. Re-review is required after P4 (attachments)
and at the P6 review gate.
