# voice-relay

Python service that owns the WebRTC leg, the local speech pipeline, the voice-brain model, and the Hermes
adapter.

Current layout (service shell):

```
relay/
  __init__.py       # package version
  app.py            # FastAPI app: lifespan, correlation ids, /health, /ready
  config.py         # typed settings loaded from the environment
  logging/          # structured logging, redaction, audit, retention, audio debug
    __init__.py     # public re-export surface
    core.py         # formatters, configure_logging, access log
    redact.py       # secret/path redaction and logging.Filter
    audit.py        # append-only audit trail
    retention.py    # age/size pruning and rotation policy
    prune.py        # python -m relay.logging.prune CLI
    audio.py        # optional WAV debug recording and log-safe summaries
  auth/             # device tokens, hash-at-rest store, FastAPI auth dependency
  storage/          # attachment inbox: sniffing, atomic storage, delegation parts
  brain/            # prompts, provenance envelopes, approval gate, narration
  api/              # HTTP routers (attachment upload)
  tests/            # in-process ASGI tests
  pyproject.toml    # package metadata and tooling config
  Dockerfile        # python:3.12-slim, non-root runtime
  hermes-talk.service  # systemd unit stub (deploy/ is canonical)
  README.md
```

Planned submodules (future tasks):

```
  transport/        # SmallWebRTC offer/answer, audio frames
  speech/           # VAD, STT, TTS adapters
  brain/            # LFM tool router, prompts, tool schemas
  hermes/           # runs/SSE adapter, approvals, stop
  storage/          # inbox/outbox, validation
```

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

## Prompt-injection containment

Untrusted attachment text and tool output are wrapped in provenance envelopes carrying the source
name, SHA-256, and a nonce, and the system preamble states that everything inside an envelope is
data, never instructions. Documents are handed over as a path plus a hash; an inlined excerpt is
truncated with an explicit marker. The spoken approval gate is the only approver, and tool narration
reads a mapped phrase with a bounded excerpt rather than raw output.
