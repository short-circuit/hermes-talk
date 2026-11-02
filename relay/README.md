# voice-relay

Python service that owns the WebRTC leg, the local speech pipeline, the voice-brain model, and the Hermes
adapter.

Current layout:

```
relay/
  __init__.py       # package version
  app.py            # FastAPI app: lifespan, correlation ids, probes, session reaper
  config.py         # typed settings loaded from the environment
  limits.py         # admission control, upload leases, token buckets, barge-in gate
  tokens.py         # python -m relay.tokens CLI
  api/              # HTTP routers: attachment upload, artifact listing/download
  auth/             # device tokens, hash-at-rest store, FastAPI auth dependency
  brain/            # router, prompts, tool schemas, delegation, approvals, narration
  hermes/           # runs/SSE adapter: submit, events, status, stop
  logging/          # structured logging, redaction, audit, retention, audio debug
  maintenance/      # retention pruning and Hermes session deletion
  speech/           # STT, TTS, PCM/Opus helpers, backpressure
  storage/          # attachment inbox, validation probe, confined artifact outbox
  transport/        # turn detection, VAD, barge-in, interruption markers
  tests/            # offline suites and replay fixtures
  pyproject.toml    # package metadata and tooling config
  Dockerfile        # python:3.12-slim, non-root runtime
  hermes-talk.service  # systemd unit stub (deploy/ is canonical)
  README.md
```

The app currently serves the probe and file-transfer surface. `transport/`, `speech/`, `brain/` and
`hermes/` are shipped libraries that the call loop composes; the WebRTC signalling endpoint is served
by `android/stub-relay/` for client tests until it is mounted here. See `docs/ARCHITECTURE.md` for the
component map, data flow and trust boundaries.

Nothing in this module may call the internet at runtime.

## Attachments

`POST` and `PUT /v1/attachments` accept one multipart `file` field and require a device token.
Uploads are capped by `MAX_ATTACHMENT_MB` (default 50 MiB) and stored under `INBOX_DIR`, which must
point at a directory outside the repository. The relay creates it with mode `0700` on first use.

The content type is decided by magic bytes only; a declared extension must belong to the sniffed
type's family. The allowlist is PNG, JPEG, WebP, GIF, PDF, ZIP, WAV, MP3, OGG/Opus, FLAC, M4A/MP4,
WebM, and plain text (`.txt`, `.md`, `.csv`). HEIC/HEIF is **not** supported: the phone must convert
to JPEG before uploading. Renamed executables (an `MZ` header) and empty uploads are rejected with a
spoken sentence. ZIP is accepted; `.docx`, `.xlsx`, and `.epub` containers must be renamed to `.zip`
before uploading.

Structural parsing of PDF, image, and zip content runs in an isolated child process with a 10 second
wall-clock timeout, CPU and address-space limits, and a minimal environment. The relay process never
decodes media itself, so a hostile attachment cannot stall or exhaust it. Untrusted content is also
bounded before it is handed to Hermes:

- images: at most 40 megapixels; JPEG, PNG, and WebP are downscaled so the long edge is at most
  1024 px, EXIF/metadata is stripped, and GIF is passed through after a header-only size check;
- PDFs: at most 200 pages and 2 MiB of extracted text; encrypted PDFs are rejected rather than
  decrypted;
- ZIP archives: at most 512 entries, 256 MiB of declared uncompressed data, and a 200:1 compression
  ratio; every member is streamed through a 64 KiB inflater so a member whose header lies is still
  caught before anything is extracted to disk.

Stored files use server-generated names, are written atomically, and carry a SHA-256 of the stored
bytes plus the detected MIME type. Before an attachment is delegated, the relay re-hashes the stored
file and refuses to use it if the bytes changed. Documents are handed to Hermes as absolute paths in
the run's text content, while images within 8 MiB are additionally inlined as `image_url` data URLs.

## Artifacts

The voice agent writes generated files under `OUTBOX_DIR`, which must point at a directory outside
the repository. The relay never creates or deletes anything there; retention pruning is a separate
maintenance job. Files are served only to an authenticated device.

`GET /v1/artifacts?limit=50` returns the unexpired artifacts visible under the outbox root. The
response is `{"artifacts": [...], "count": N, "limit": L, "retention_days": D}`; each artifact has
`id` (relative POSIX path), `name`, `mime`, `size_bytes`, `sha256`, `created_at`, and `expires_at`
(`null` when retention is disabled). No absolute path is returned. `limit` must be between 1 and 200
and the listing is newest first.

`GET` and `HEAD /v1/artifacts/{artifact_id}` stream one file from the open descriptor in 64 KiB
chunks. Responses set `Content-Type`, `Content-Length`, `Content-Disposition: attachment`,
`ETag: "<sha256>"`, `Last-Modified`, `X-Artifact-SHA256`, `X-Artifact-Id`,
`X-Content-Type-Options: nosniff`, and `Cache-Control: private, max-age=0, must-revalidate`.
`If-None-Match` with the current ETag returns `304` with no body. HEAD returns the same headers with
an empty body. Range requests are not supported in v1.

Both endpoints require a device token; a missing or invalid token is `401`. Artifact ids are
relative POSIX paths. Absolute paths, backslashes, control characters, empty/dot/hidden segments,
directories, missing files, symlinks anywhere in the chain, and expired files are all `404`.
Unknown, missing, symlinked, and expired artifacts deliberately share the same status; only the
`error` code differs (`artifact_not_found` or `artifact_expired`). When `OUTBOX_DIR` is unset or
inside the repository the response is `503 outbox_not_configured`; an unreadable root is
`503 outbox_unavailable`. Errors include a `spoken` sentence. `GET` and `HEAD /v1/artifacts`
inherit the per-device `download` rate limit.

Expiry is enforced on both listing and download: with `OUTBOX_RETENTION_DAYS` greater than zero, an
artifact whose mtime is at least that many days old is treated as expired. Setting it to `0` keeps
artifacts until manual deletion. The serving path ignores dot-prefixed entries such as `.locks/`.

## Prompt-injection containment

Untrusted attachment text and tool output are wrapped in provenance envelopes carrying the source
name, SHA-256, and a nonce, and the system preamble states that everything inside an envelope is
data, never instructions. Documents are handed over as a path plus a hash; an inlined excerpt is
truncated with an explicit marker. The spoken approval gate is the only approver, and tool narration
reads a mapped phrase with a bounded excerpt rather than raw output.
