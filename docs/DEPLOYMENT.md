# Deployment draft

## Hosts

| Service | Bind | Exposure |
|---|---|---|
| Hermes API server | `127.0.0.1:8642` | loopback only |
| voice-relay | `0.0.0.0:8790` | LAN, TLS via reverse proxy |
| local speech/inference | `127.0.0.1:8081` | loopback only |

## Environment

All configuration comes from `.env` (see `.env.example`). No secrets in compose files or images.

## Device tokens

Device tokens are hash-at-rest. The relay stores only a SHA-256 digest, the public token id prefix,
the device label, the issue time, and the revocation time. Plaintext is shown once by the admin CLI
and never persisted. `DEVICE_TOKENS` remains a deprecated bootstrap fallback and is consulted only
while the state file has no active tokens.

### State file and permissions

The state file defaults to `$DATA_DIR/device_tokens.json`; set `TOKEN_STATE_PATH` to place it
elsewhere. Keep it outside the git work tree — the CLI refuses a path inside one, and the service
logs a warning and keeps running.

- The parent directory is created with mode `0700` if missing.
- The state file is written with mode `0600`; a looser existing mode is repaired to `0600` on load.
- Writes are atomic: a locked temp file in the same directory is fsynced and replaced in one step.
  Rotation writes the successor and the revoked predecessor in a single replace.
- Back up the hash file (it is enough to restore every device token). A backup does not contain
  plaintext, so losing a token is only recoverable by rotating it.

### Issue, rotate, and revoke

Run these on the relay host. They are loopback-only by nature: they read and write local state and
make no network calls. `DATA_DIR` must be set or pass `--state-path`.

```
# Issue a token for a phone; copy the plaintext line into the device Keystore.
python -m relay.tokens issue --label phone

# Rotate a device; the predecessor stops working on the next request.
python -m relay.tokens rotate --label phone

# Revoke a lost device by label or by token id.
python -m relay.tokens revoke --label phone
python -m relay.tokens revoke --id 0123abcd

# List records (never plaintext).
python -m relay.tokens list
```

Revoke and rotate take effect without a process restart: the relay re-stats the state file on every
verification and reloads it when `(mtime_ns, size, inode)` changes. No restart, no env edit.

### Request authentication

The relay accepts a device token only from a single `Authorization: Bearer <token>` header. Query
strings and cookies are ignored by construction. Missing or invalid credentials receive `401` with
`WWW-Authenticate: Bearer` and a body of the form:

```json
{"error": "unauthorized", "reason": "missing_credentials"}
```

Coarse reasons are `missing_credentials`, `invalid_scheme`, `invalid_token`, and
`no_tokens_configured`. Detailed reasons (`missing_header`, `invalid_scheme`, `malformed_token`,
`unknown_token`, `no_tokens_configured`, `auth.state_error`) appear only in audit lines on
`relay.audit`, with the client host and path. Token material is never logged: audit lines carry the
device label and the 8-character token id prefix only. uvicorn's own access log is disabled, so only
the relay's access log (method, path, status, no query string) is written.

### Deprecated bootstrap fallback

`DEVICE_TOKENS` is a comma-separated list of plaintext tokens, kept for migration. It is ignored as
soon as the state file has at least one active token. When it authenticates, the relay emits one
`auth.bootstrap_tokens_active` warning per store instance. Remove it once every device has been
issued a token through the CLI.

## Logging, audit, and retention

The relay writes only to local storage. No log file is ever created inside the repository, even when
`DATA_DIR` points at the checkout: the file sink is skipped and no directory is created.

| Data | Path | Notes |
|---|---|---|
| Application log | `$DATA_DIR/logs/relay.log` | rotating, redacted, local only |
| Audit trail | `$DATA_DIR/audit.log` | append-only JSONL, mode `0600` |
| Audio debug recordings | `$DATA_DIR/recordings/` | only when `RECORD_AUDIO=1` |

Audit entries record who approved or denied what, when, and against which transcript hash. They are
written with `O_APPEND` and one `os.write` per entry; earlier bytes are never rewritten. Rotation only
renames whole files, and pruning deletes whole files. The active audit file is never truncated.

Secrets (`HERMES_API_KEY`, `HERMES_SESSION_KEY`, every `DEVICE_TOKENS` entry) and absolute path roots are
scrubbed at the logging boundary, including tracebacks. Raw audio frames are never logged; only a byte
count and SHA-256 prefix are emitted.

### Retention knobs

Set these in `.env` (defaults shown):

```
LOG_TRANSCRIPT_MAX_CHARS=280
LOG_MAX_AGE_DAYS=14
LOG_MAX_BYTES=10485760
LOG_BACKUP_COUNT=5
AUDIT_MAX_AGE_DAYS=90
AUDIT_MAX_BYTES=5242880
AUDIT_BACKUP_COUNT=10
```

### Pruning

Run retention offline from the repository root:

```
make prune-logs
make prune-logs PRUNE_ARGS="--dry-run"
make prune-logs PRUNE_ARGS="--json --max-age-days 30"
```

The CLI reads `DATA_DIR` and the retention settings from the environment, accepts `--data-dir` to
override the location, and never makes a network call.

A daily cron example (adjust the checkout path for your host):

```
17 4 * * * cd /opt/hermes-talk && make prune-logs
```

`RECORD_AUDIO` is off by default. When enabled, recordings are written under `$DATA_DIR/recordings` and
never enter the repository.

## Compose (draft)

```yaml
services:
  voice-relay:
    build: ../relay
    network_mode: host
    env_file: .env
    volumes:
      - ${INBOX_DIR}:/inbox
      - ${OUTBOX_DIR}:/outbox
    restart: unless-stopped
```

## Reverse proxy (draft)

```
voice.example.lan {
    tls internal
    reverse_proxy 127.0.0.1:8790
}
```

## Rollback

Keep the previous image tag; redeploy with `docker compose up -d --no-build` after retagging. The relay is
stateless apart from inbox/outbox, so rollback does not lose transcripts.

## Limits and admission control

The relay is single-user on a LAN but reachable by every host on it, so every path that can cost
time, memory, or bandwidth is admitted through `relay.limits`. Limits come only from `Settings`;
there are no per-request overrides and no network calls in the admission path.

| Env key | Default | Effect | Rejection |
|---|---|---|---|
| `MAX_CONCURRENT_SESSIONS` | `3` | Concurrent call sessions | `503` `session_limit_reached` |
| `MAX_CONCURRENT_RUNS` | `2` | Concurrent runs across all devices | `503` `run_limit_reached` |
| `MAX_CONCURRENT_RUNS_PER_DEVICE` | `1` | Concurrent runs for one device | `503` `run_limit_reached` |
| `MAX_CONCURRENT_UPLOADS` | `2` | In-flight attachment uploads | queued, then `429` `upload_queue_full` |
| `MAX_QUEUED_UPLOADS` | `8` | Bounded FIFO upload queue | `429` `upload_queue_full` |
| `SESSION_IDLE_TIMEOUT_SECONDS` | `120.0` | Idle deadline before a session is reaped | session stopped, capacity freed |
| `SESSION_REAPER_INTERVAL_SECONDS` | `5.0` | Reaper cadence | none |
| `RATE_LIMIT_RUN_PER_MINUTE` | `20` | Per-device run budget | `429` `rate_limited` |
| `RATE_LIMIT_RUN_BURST` | `5` | Run burst (must not exceed the minute budget) | `429` `rate_limited` |
| `RATE_LIMIT_UPLOAD_PER_MINUTE` | `30` | Per-device upload budget | `429` `rate_limited` |
| `RATE_LIMIT_UPLOAD_BURST` | `10` | Upload burst | `429` `rate_limited` |
| `RATE_LIMIT_DOWNLOAD_PER_MINUTE` | `120` | Per-device download budget | `429` `rate_limited` |
| `RATE_LIMIT_DOWNLOAD_BURST` | `30` | Download burst | `429` `rate_limited` |
| `RATE_LIMIT_MAX_DEVICES` | `64` | Cap on tracked per-device buckets (LRU) | none |
| `MAX_SSE_FRAME_BYTES` | `65536` | SSE frame cap | `FrameTooLargeError` -> final error frame |
| `SSE_IDLE_TIMEOUT_SECONDS` | `30.0` | Per-frame idle timeout | run stop, then final error frame |
| `SSE_MAX_RECONNECT_ATTEMPTS` | `5` | Reconnects per session | `429` `reconnect_budget_exhausted` |
| `BARGE_IN_MIN_SPEECH_MS` | `300` | Ignore shorter speech | suppressed, no stop |
| `BARGE_IN_COOLDOWN_MS` | `1000` | One stop per cooldown window | suppressed, no stop |
| `REJECT_RETRY_AFTER_SECONDS` | `2` | Advertised `Retry-After` on rejection | response header |

Rate limiting uses a per-device token bucket keyed by the public token id (8 hex characters, never
the plaintext token) and by category. Burst is the bucket capacity; refill is
`per_minute / 60` tokens per second. A rejected request carries `Retry-After` with the ceiling of
the seconds until one token refills. Buckets are held in an LRU capped at `RATE_LIMIT_MAX_DEVICES`;
a bucket that refilled fully after an idle period is dropped instead of retained, so memory stays
flat under churn.

Session, run, and upload capacity is bounded the same way. The upload queue is FIFO: releasing an
in-flight lease promotes the oldest queued lease, and a queue at `MAX_QUEUED_UPLOADS` rejects rather
than grows. `/ready` reports the informational `limits` check with active/max sessions, runs, and
`N in flight, Q/M queued` uploads; it never includes token values or absolute paths.

SSE hygiene applies to run streams: frames above `MAX_SSE_FRAME_BYTES` are rejected, a frame that
arrives later than `SSE_IDLE_TIMEOUT_SECONDS` stops the run and closes the source, and reconnects are
bounded per session. Closing a session runs its registered stop hooks exactly once; the reaper closes
sessions past their idle deadline and shutdown calls `close_all` before logging completes.

Barge-in is debounced: speech shorter than `BARGE_IN_MIN_SPEECH_MS` is ignored, and after an accepted
interruption further interruptions are suppressed until `BARGE_IN_COOLDOWN_MS` elapses. A noisy room
therefore produces one stop per utterance instead of a storm of stops.

### Spoken contract

Every admission rejection and idle stop carries a short `spoken` sentence in the response body or
final SSE frame. The client must render `spoken` through local text-to-speech; the relay never
silently drops audio. Bodies have the shape:

```json
{"error": "rate_limited", "detail": "...", "spoken": "One moment — the relay is handling too many requests.", "retry_after": 2}
```

`correlation_id` is included when the relay assigned one. No token material is ever present.

### Offline soak

Run the simulated ten-minute soak in-process (fake clock, no sockets):

```
.venv/bin/python -m pytest -q relay/tests/test_limits.py -k soak
```

Look for: rejections are all `429`, tracked buckets stay at or below `RATE_LIMIT_MAX_DEVICES`, the
upload queue stays at or below `MAX_QUEUED_UPLOADS`, and traced memory growth stays under 512 KiB.
That is the signal that the relay rejects excess instead of accumulating it.

### Extending the relay

New endpoints must live under the documented prefixes in `RATE_LIMIT_ROUTES` (`/v1/runs`,
`/v1/attachments`, `/v1/artifacts`) to inherit per-device rate limits. Capacity leases for new work
come from `app.state.limits` (`acquire_session`, `acquire_run`, `acquire_upload`), never from ad-hoc
counters; the same object supplies the SSE codec, idle guard, reconnect budget, and barge-in gate.

## Data retention and deletion

Retention is opt-in.  The code defaults for every window are `0`, which means
"keep until an operator deletes it by hand"; nothing is removed unless you set a
window.  The documented conservative values live in `.env.example`, not in code:

| Variable | Documented value | Effect when set |
|---|---|---|
| `INBOX_RETENTION_DAYS` | `30` | Unlink inbox attachments older than 30 days |
| `OUTBOX_RETENTION_DAYS` | `14` | Remove outbox artifacts older than 14 days |
| `TRANSCRIPT_RETENTION_DAYS` | `90` | Delete `api_server` Hermes sessions idle for 90 days |

Set them in `.env` (or the service environment) and restart, or override them
per run with the CLI flags described below.  A negative value is rejected at
startup; `0` disables pruning for that store.

### What the pruner does

```
python -m relay.maintenance prune [--dry-run] [--json]
```

Per-run overrides: `--inbox-days`, `--outbox-days`, `--transcript-days`,
`--inbox-dir`, `--outbox-dir`, and `--now` (ISO 8601, for replays).  These flags
win over the environment for that run only.

- **Inbox** (`$INBOX_DIR`): walks the root without following symlinks and
  unlinks regular files whose mtime is at or past `INBOX_RETENTION_DAYS`.
  Crash leftovers such as `.incoming-*.part` are ordinary candidates.  A file
  claimed by a running turn is skipped (`reason=in_use`).
- **Outbox** (`$OUTBOX_DIR`): the same age rule for artifacts in nested
  directories.  Symlinks are neither followed nor deleted, so a link to a file
  outside the root leaves its target untouched.
- **Transcripts**: lists sessions from the loopback Hermes API
  (`GET /api/sessions?source=api_server`) and deletes expired rows with
  `DELETE /api/sessions/{id}`.  The Hermes session database is never written
  directly.  Only rows whose `source` is exactly `api_server` are eligible;
  Telegram, CLI, and Kanban sessions belong to other surfaces and are left
  alone.

Roots that resolve inside the repository are refused: the pruner records an
error and deletes nothing inside the checkout.

Run the command from the repository root (`cd /opt/hermes-talk`), not from
inside `relay/`: a directory named `logging` on `sys.path` shadows the standard
library module of the same name.  The container image has the same problem with
its `WORKDIR /app`, so run the pruner there with `-w /`:

```
docker run --rm -w / --env-file /etc/hermes-talk/relay.env <image> python -m relay.maintenance prune
```

### Active-call safety

A turn that hands an inbox attachment to the agent holds a cross-process claim
with `ReferenceGuard` for the whole turn.  The pruner uses the same
`<root>/.locks` claims: an expired file that is currently claimed is skipped
and retried on the next run.  A file is unlinked only while the pruner holds the
exclusive lock, so a turn cannot start reading a file mid-deletion.  Hermes
answers `409 session_active_turn` for a session with a running turn; the pruner
records `reason=active_turn` and leaves it for the next run.

### Idempotency, dry runs, and exit codes

Pruning is idempotent: a second run over the same tree deletes nothing and
emits no new `retention.deleted` entries.  `--dry-run` performs no unlink and
issues no `DELETE` calls, but still reports the planned items.  The process
exits `0` when no error was recorded and `1` when any error was recorded (a dry
run follows the same rule).  Use `--json` for a machine-readable report; the
default output is a human summary that never contains absolute paths.

### Audit trail

Every deletion is appended to `$DATA_DIR/audit.log` (mode `0600`):

- `retention.deleted` — `kind`, relative `path` or `session_id`, `age_days`,
  `reason`, and `size_bytes` for files only;
- `retention.skipped` — `kind`, relative `path` or `session_id`, `age_days`,
  `reason`;
- `retention.error` — `kind`, relative `path` or `session_id`, `error`;
- `retention.summary` — counts, `dry_run`, and `stale_locks_removed`.

Audit entries carry paths relative to the pruned root, ages, sizes, and ids.
They never contain file contents, transcript text, or absolute home paths.

### Scheduling

A cron example (source the relay environment so `$DATA_DIR` is set):

```
# /etc/cron.d/hermes-talk-retention
15 3 * * * relay sh -c 'set -a; . /etc/hermes-talk/relay.env; set +a; cd /opt/hermes-talk && .venv/bin/python -m relay.maintenance prune >> "${DATA_DIR:-/var/lib/hermes-talk}/logs/retention.log" 2>&1'
```

`configure_logging` creates `${DATA_DIR:-/var/lib/hermes-talk}/logs` once the
relay has run at least once, so the redirect target exists for the cron job.

The equivalent systemd units (documented here, not installed by the project):

```
# /etc/systemd/system/hermes-talk-retention.service
[Unit]
Description=Hermes Talk data retention
After=network.target

[Service]
Type=oneshot
User=relay
EnvironmentFile=/etc/hermes-talk/relay.env
WorkingDirectory=/opt/hermes-talk
ExecStart=/opt/hermes-talk/.venv/bin/python -m relay.maintenance prune
```

```
# /etc/systemd/system/hermes-talk-retention.timer
[Unit]
Description=Nightly Hermes Talk data retention

[Timer]
OnCalendar=*-*-* 03:15:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable with `systemctl enable --now hermes-talk-retention.timer`.

### Manual deletion

While the relay is idle, remove files directly inside `$INBOX_DIR` or
`$OUTBOX_DIR` with `rm`.  Delete a transcript only through the API, never by
editing the Hermes database:

```
curl -X DELETE -H "Authorization: Bearer $HERMES_API_KEY" \
  http://127.0.0.1:8642/api/sessions/<session-id>
```

Manual deletions are permanent and are **not** written to the audit trail:
only the automated pruner appends `retention.*` entries.

`python -m relay.maintenance prune` is offline apart from the loopback Hermes
session call; inbox and outbox pruning touch only the local filesystem.
