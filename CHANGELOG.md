# Changelog

All notable changes to Hermes Talk are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) from v0.1.0 onward.

Because this is a pre-1.0 project, a minor bump may contain breaking changes; they are called out under
**Changed** with a migration note.

## 0.1.0 — 2026-11-03

First tagged release. It is a pre-alpha baseline: the relay's probe and file-transfer surface, the Hermes
run adapter, the spoken approval gate and the Android client are shipped and tested; the call loop that
composes transport, speech, brain and Hermes into the relay app is not mounted yet (see **Known
limitations**).

### Added

**Relay service**

- FastAPI service shell with typed settings loaded from the environment, request correlation ids,
  `/`, `/health`, `/ready` and `/v1/whoami`, structured JSON logging with transcript redaction, and an
  audit trail whose entries carry hashes rather than content.
- Attachment inbox: `POST`/`PUT /v1/attachments` with per-device bearer auth, magic-byte type detection,
  a declared-extension family check, size caps, EXIF/metadata stripping, image downscaling, and a
  confined storage layout outside the repository.
- Attachment parsing isolated in a child process with a wall-clock timeout, CPU and address-space
  limits, a minimal environment, and hash re-verification after the probe returns.
- Artifact outbox: `GET`/`HEAD /v1/artifacts/{id}` with authenticated, expiring downloads and confined
  path resolution.
- Device tokens: `python -m relay.tokens issue|list|rotate|revoke` with a hash-at-rest store, no-restart
  semantics, and a deprecated `DEVICE_TOKENS` bootstrap fallback that is consulted only while no active
  token exists.
- Admission control and per-device token buckets for runs, uploads and downloads, with `429`/`503`
  responses that carry a short spoken sentence and a `Retry-After` value.
- Retention pruning for inbox, outbox, logs, audit and transcripts, plus Hermes session deletion, with
  every deletion audited. Retention is disabled by default (`0` keeps data).
- Speech stack as shipped libraries: sherpa-onnx and faster-whisper STT with finalization and a
  first-segment cap, kokoro-onnx TTS with bounded pending sentences, PCM/Opus conversion, backpressure,
  turn detection, barge-in flush and interruption markers, plus an audio-debug recorder that is off by
  default.
- Voice-brain router: four tools, a local direct-answer policy, streamed tool calls, and typed
  `RouterUnavailable` failures.
- Hermes adapter: run submission, SSE event decoding and normalization, status polling, stop control,
  and typed errors. The configured request timeout is enforced on submit, status and the events
  fallback, so a hung gateway cannot block the caller indefinitely.
- Delegation lifecycle: async delivery, idempotent retries, and throttled tool-activity narration with
  redaction. Delegation submits attachment content parts (text plus `image_url`) to Hermes, validating
  each part before the request and mapping inbox failures to a typed delegation failure.
- Spoken approval gate: a confirmation phrase that must name the action, a confidence floor, a
  read-only allowlist, `ControlId` binding, a bounded settled-request registry, an echo/playback window
  while TTS is speaking, `max_reasks` followed by `reask_exhausted`, and exactly-once posting of the
  approval decision.

**Android client**

- Kotlin/Compose app with an RTVI client built on `ai.pipecat:small-webrtc-transport`, a relay
  certificate pin, an encrypted token store, a call state machine with teardown that releases the
  microphone and audio focus, and screen-off call support.
- On-device wake word using sherpa-onnx keyword spotting, with push-to-talk as the fallback, and the
  measured detection/false-trigger/battery numbers recorded in `docs/research`.
- Attachments module: durable upload queue with backoff and resume-after-restart, artifact listing and
  download, and share-target wiring into the app.
- Instrumentation suite: in-process loopback fixture for upload retry, token expiry and call-state
  tests, plus an end-to-end flow against the Python stub relay; unit tests across the modules.

**Deployment and operations**

- Docker Compose stack (relay, Caddy with an internal CA, model mounts) and a systemd alternative, with
  loopback-only relay binding and TLS terminated at the proxy.
- Operator runbook: start/stop and drain order, health/TLS/auth checks, speech warm-up, token lifecycle,
  log locations and pruning, backup/restore, and a failure table. The gateway is never restarted without
  its owner's agreement.
- Documented rollback procedure and image pinning by git short sha.

**Documentation**

- Architecture guide with a repository map, component tables, ASCII topology and data-flow diagrams,
  and eight trust boundaries mapped to enforcing code.
- HTTP and RTVI API reference with executed request/response examples, an error catalogue traced to the
  raising classes, and admission/rate-limit tables.
- Threat model, privacy and data-flow statement, testing strategy and latency SLOs, deployment guide,
  ADRs for local-only operation and transport, and the research notes behind the model and stack
  choices.

**Repository gates**

- `scripts/check_repo_hygiene.sh` enforces seven documented rules (personal emails, home paths, RFC 1918
  addresses, secret-shaped tokens and PEM keys, oversized files, committed `.env` files, private-suffix
  hostnames) across tracked and untracked non-ignored files, with allowlists and a `--self-test` that
  proves each rule fires on a planted fixture.
- CI runs gitleaks over the full history on every branch and pull request, the hygiene gate self-test
  before the real scan, Python lint/format/tests, Android unit tests, and an emulator instrumentation
  job. Pre-commit mirrors the secret and hygiene gates locally.
- `.gitleaks.toml` keeps the default ruleset and allowlists the documentation, test-fixture and
  probe-script paths whose curl examples and identifiers carry synthetic placeholders; the allowlist is
  path-scoped, and a token planted anywhere else still fails the scan.

### Changed

- `relay/pyproject.toml` now declares the SPDX licence expression plus a licence file, so built wheels
  carry the MIT text and a `License-File: LICENSE` entry. `relay/LICENSE` is a byte-identical copy of the
  repository `LICENSE` (CI compares them) because setuptools rejects `..` in licence globs. The build
  backend floor is `setuptools>=77`, which the string licence field and `license-files` require (PEP 639).
- Project metadata carries an author name only; the repository deliberately contains no personal email
  address.

### Fixed

- The Python CI job was red before this release: 64 lint findings and 5 unformatted files in
  `scripts/`, `spike/` and `android/stub-relay/` (implicit string concatenation in collection literals,
  missing `check=` on `subprocess.run`, unused `noqa` directives, PEP 604 annotations, a blind except and
  a `try/except/pass` in fixture code). Fixed without changing behaviour: the kanban seed script's
  `--dry-run` output is byte-identical before and after.
- `HermesClient._request` passed `timeout=None` by default, which disabled the configured httpx timeout
  on submit and status calls; a hung gateway blocked the caller forever. The timeout is now enforced and
  covered by a regression test.
- Compose mounted `/tmp` with Docker's default `noexec`, so phonemizer's copy of `libespeak-ng.so` could
  not be mapped and every TTS synthesis failed. `/tmp` is now `exec`.
- `DEPLOYMENT.md` and `README.md` told operators to copy `.env.example` from `deploy/`, but the example
  lives at the repository root; both now point at `../.env.example`.
- `python -m relay.tokens revoke --label` could miss the active token after a rotation; the CLI now
  targets the active successor. The runbook documents the `--id` workflow as the safe alternative.
- The full-history gitleaks scan reported 19 false positives at the tag — `Authorization:` headers in
  curl examples and `*_KEY`/`*-Key` identifiers in `docs/`, `relay/tests/` and
  `scripts/probe_hermes_api.sh` — which would have failed CI. `.gitleaks.toml` now allowlists those three
  paths only, and a planted `ghp_` token outside them still fails the scan.
- `SECURITY.md` pointed at maintainer email addresses in `pyproject.toml` that did not exist; it now
  directs reports to a private GitHub advisory.

### Known limitations

- The call loop that composes transport, speech, brain and Hermes into the relay app is not mounted.
  `relay.app` serves the probes plus `/v1/attachments` and `/v1/artifacts`; the RTVI signalling endpoint
  used by the Android client is served by `android/stub-relay`, which is a fixture, not the production
  app.
- `POST /v1/runs` is reserved in the rate-limit table but not mounted; its request/response schemas are
  documented in `docs/research/hermes-api.md` §3 and will move into `docs/API.md` when it is mounted.
- `MAX_CALL_MINUTES` is read by the configuration but not enforced by any module yet.
- Android `versionCode` stays at `1` for this tag; release builds are not signed with a release key yet.

### Upgrade notes

None — this is the first tagged release.
