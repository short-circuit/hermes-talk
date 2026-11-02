# Deployment

The relay runs on one host, next to the Hermes API server and the local inference stack. Nothing here
reaches the internet: every runtime dependency is a loopback service on the same machine, and the only
LAN-facing listener is the reverse proxy.

Two supported paths, both shipped in this repository:

| Path | Files | Use when |
|---|---|---|
| Docker Compose (primary) | `deploy/compose.yaml`, `deploy/Caddyfile` | you want a reproducible stack from the repo plus an `.env` |
| systemd (alternative) | `deploy/hermes-talk.service`, `deploy/Caddyfile` | the host already runs the inference services natively |

Run one or the other, never both: each binds `127.0.0.1:8790`. In both cases TLS terminates at Caddy
with an internal CA and the relay binds loopback only.

## Topology

```
phone --HTTPS/TLS--> Caddy (443) --HTTP--> relay (127.0.0.1:8790) --HTTP--> Hermes (127.0.0.1:8642)
                                                  |  `--HTTP--> voice-brain (127.0.0.1:8080)
                                                  `-- /var/lib/hermes-talk/{inbox,outbox,logs,audit.log,device_tokens.json}
models (read-only) --------------------------------> /models inside the relay container
```

The relay never calls a cloud service. Speech recognition, speech synthesis, and the conversational
router all run locally (see `docs/adr/0001-local-only.md`).

## Host requirements

- Linux host, x86_64 or aarch64. Docker Engine 24+ with Compose v2 for the compose path, or systemd 245+
  and Python 3.12 for the systemd path.
- The Hermes API server reachable at `HERMES_BASE_URL` (loopback).
- The voice-brain server (llama.cpp with `--jinja`) reachable at `VOICE_BRAIN_BASE_URL` (loopback).
- The speech models, kept outside the repository (see [Models](#models)).
- A name or hosts-file entry for `RELAY_SITE_ADDRESS` on every client device (see
  [Reverse proxy and internal TLS](#reverse-proxy-and-internal-tls)).
- No internet access at runtime. The compose stack pulls images once at deploy time; after that it runs
  offline.

## Port table

| Port | Proto | Service | Bound to | Exposure | Purpose |
|---|---|---|---|---|---|
| 443 | TCP | Caddy | `0.0.0.0` | LAN | TLS entry point, certificate from the internal CA |
| 80 | TCP | Caddy | `0.0.0.0` | LAN | HTTP to HTTPS redirect only |
| 8790 | TCP | relay | `127.0.0.1` | host only | uvicorn: `/health`, `/ready`, `/v1/*` |
| 8642 | TCP | Hermes API server | `127.0.0.1` | host only | agent runs, SSE events, sessions |
| 8080 | TCP | voice-brain router | `127.0.0.1` | host only | OpenAI chat-completions tool router |
| 8081 | TCP | LocalAI | `127.0.0.1` | host only | auxiliary inference endpoints |
| 2019 | TCP | Caddy admin API | `127.0.0.1` (container) | none | healthcheck and local config reload |

No UDP port is published. WebRTC media stays peer-to-peer on the LAN or tailnet
(`docs/adr/0002-transport.md`).

Firewall: allow 443 and 80 from the LAN only. Nothing else needs to be reachable from outside the host.

## Configuration

Every value comes from `.env`, including every secret. No secret is inlined in `compose.yaml` or baked
into an image.

```
cd deploy
cp ../.env.example .env
chmod 600 .env
$EDITOR .env
```

Fill in at least:

- `HERMES_API_KEY` and `HERMES_SESSION_KEY` — real values, never committed.
- `DEVICE_TOKENS` — a bootstrap token, or leave it empty once tokens are issued through the CLI.
- `RELAY_SITE_ADDRESS` — the name the proxy serves.
- `HERMES_TALK_MODELS` — the host directory with the model weights (compose path).
- `RELAY_IMAGE` — the image tag; pin the git short sha for a rollback target.

Storage paths default to `/var/lib/hermes-talk/...` and must stay outside the repository. The relay
refuses to create log files or inbox roots inside the checkout.

`RELAY_HOST` must be `127.0.0.1`: the relay does not terminate TLS, so binding `0.0.0.0` would publish
plaintext HTTP on the LAN. The compose file pins the bind address and the storage roots explicitly, so a
host-specific `.env` cannot move them.

## Models

Model weights are gitignored and never committed. For the compose path the directory named by
`HERMES_TALK_MODELS` must contain:

```
sherpa-onnx-streaming-zipformer-en-2023-06-26/   STT_MODEL_DIR
faster-whisper-base/                            WHISPER_MODEL_DIR
kokoro-v1.0.int8.onnx                           TTS_MODEL_PATH
voices-v1.0.bin                                 TTS_VOICES_PATH
```

The directory is mounted read-only at `/models`, and the compose file points each setting at the mounted
path. For a systemd install, set the same variables in `/etc/hermes-talk/relay.env` to their host paths
(for example `/srv/hermes-talk/models/kokoro-v1.0.int8.onnx`).

`/ready` does not open the model files; it only checks that the model names are configured. Loading
happens on the first call, and `StreamingTts.warmup()` is how you pay that cost ahead of time. Measured
cold start and per-turn latency are in `docs/research/speech-latency.md`.

## Deploy with Docker Compose

### 1. Prepare the host

```
git clone <repository> /opt/hermes-talk
cd /opt/hermes-talk/deploy
cp .env.example .env && chmod 600 .env
$EDITOR .env
```

Place the models in `HERMES_TALK_MODELS` using the layout above.

### 2. Validate the proxy configuration before starting anything

```
docker run --rm --env-file .env \
  -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" \
  caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

The command must print `Valid configuration`. It makes no network call: `tls internal` is issued locally.

### 3. Build and deploy

Pin the image to the current revision so the tag names a rollback target:

```
RELAY_IMAGE=hermes-talk-relay:$(git -C .. rev-parse --short HEAD) \
  docker compose up -d --build
```

The stack starts `volume-init` first. It prepares the data volume for uid 10001, because a fresh Docker
volume is owned by root and the relay image runs as non-root. The relay starts only after that one-shot
exits successfully; the proxy starts only after the relay reports healthy.

### 4. Health gate

```
docker compose ps
curl -fsS http://127.0.0.1:8790/health
curl -fsS http://127.0.0.1:8790/ready
```

`/health` returns `{"status":"ok",...}` and `/ready` returns `{"status":"ready",...}` with HTTP 200. A 503
from `/ready` lists the failing check; see [Health checks](#health-checks). The same gate is enforced by
the container healthcheck, so `docker compose ps` shows `healthy` only when both pass.

Then verify the TLS entry point with the proxy's own root certificate:

```
docker compose cp proxy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
curl -fsS --cacert ./caddy-root.crt \
  --resolve voice.example.lan:443:127.0.0.1 https://voice.example.lan/health
```

`voice.example.lan` is the placeholder name; substitute your `RELAY_SITE_ADDRESS`. The `--resolve` flag
proves the certificate chain without needing DNS for the rehearsal host.

### 5. Issue device tokens

```
docker compose exec relay python -m relay.tokens issue --label phone
```

Copy the plaintext line into the device keystore; it is shown once and never stored. Once the state file
has an active token, `DEVICE_TOKENS` in `.env` is inert and can be deleted. Token administration,
rotation, and revocation are in [Device tokens](#device-tokens); revoking and rotating take effect
without a restart.

### 6. Trust the internal CA on the device

Install `caddy-root.crt` as a user CA certificate on the phone (Android: *Settings → Security →
Encryption & credentials → Install a certificate → CA certificate*). Without it the client rejects the
connection. Details and alternatives are in
[Reverse proxy and internal TLS](#reverse-proxy-and-internal-tls).

### What the compose file does

- `env_file: .env` is the only source of configuration and secrets; `environment:` pins container-side
  paths and the loopback bind address, never a secret.
- `network_mode: host` lets the relay reach the host's loopback services (Hermes, voice-brain) exactly as
  the systemd unit does. A bridged container cannot see the host's `127.0.0.1`.
- `read_only`, `tmpfs`, `cap_drop: ALL`, and `no-new-privileges` keep the relay unable to write anywhere
  except its data volume and `/tmp`.
- The healthcheck probes `/health` and `/ready`, so a half-configured host stays `unhealthy` instead of
  looking green.

## Deploy with systemd

The unit in `deploy/hermes-talk.service` is the canonical systemd deployment; the copy at
`relay/hermes-talk.service` is a stub kept next to the package for reference.

### 1. Create the service user and check out the repository

```
sudo useradd --system --home-dir /opt/hermes-talk --shell /usr/sbin/nologin hermes-talk
sudo install -d -o hermes-talk -g hermes-talk /opt/hermes-talk
sudo -u hermes-talk git clone <repository> /opt/hermes-talk
```

### 2. Install the runtime

```
sudo -u hermes-talk python3.12 -m venv /opt/hermes-talk/.venv
sudo -u hermes-talk /opt/hermes-talk/.venv/bin/pip install -e "/opt/hermes-talk/relay[speech]"
```

### 3. Install the environment file

```
sudo install -d -m 0750 /etc/hermes-talk
sudo install -m 0600 -o root -g root .env.example /etc/hermes-talk/relay.env
sudoedit /etc/hermes-talk/relay.env
```

`/etc/hermes-talk/relay.env` is read by systemd as root before privileges drop, so mode `0600` is
correct. Set `RELAY_HOST=127.0.0.1` and the host model paths here.

### 4. Prepare the state directory

`StateDirectory=hermes-talk` creates `/var/lib/hermes-talk` owned by the service user at first start. The
relay creates its inbox and log directory itself but never creates the outbox, so create it:

```
sudo install -d -o hermes-talk -g hermes-talk -m 0700 /var/lib/hermes-talk/outbox
```

### 5. Install and start the unit

```
sudo install -m 0644 deploy/hermes-talk.service /etc/systemd/system/hermes-talk.service
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-talk.service
```

Starting a unit is a state change on a live host: get the operator's go before enabling it. The unit
hardens the process (`ProtectSystem=strict`, `ProtectHome=yes`, `NoNewPrivileges=yes`, a
`@system-service` syscall filter) and writes nothing outside `/var/lib/hermes-talk`.

### 6. Put Caddy in front

Install the distribution's Caddy package, install `deploy/Caddyfile` as `/etc/caddy/Caddyfile` with
`RELAY_SITE_ADDRESS` substituted for the real name, then:

```
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
```

Reload, not restart: a reload keeps existing connections and does not drop a call in progress. With the
package installation the internal CA root lives under the Caddy user's data directory
(`/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt` on Debian-style packages); confirm
the path with `caddy environ` before exporting it.

### 7. Health gate

```
systemctl is-active hermes-talk.service
curl -fsS http://127.0.0.1:8790/health
curl -fsS http://127.0.0.1:8790/ready
curl -fsS --cacert ./caddy-root.crt --resolve voice.example.lan:443:127.0.0.1 \
  https://voice.example.lan/health
```

## Reverse proxy and internal TLS

`deploy/Caddyfile` serves one site with `tls internal`, Caddy's local certificate authority:

- No ACME, no outbound HTTP call, no public DNS: the certificate is issued on the host.
- The site name comes from `RELAY_SITE_ADDRESS`; nothing about the LAN is hard-coded.
- The upstream is `127.0.0.1:8790` with `flush_interval -1`, so SSE frames and partial transcripts are
  forwarded as they are produced rather than buffered.
- There is no `encode` directive: gzip buffering breaks streaming event frames, and the audio path is
  already Opus.
- HSTS is set for one year; `X-Content-Type-Options: nosniff` is added, and the `Server` header is
  dropped.

Clients must trust the internal CA. Export the root certificate from the proxy:

```
docker compose cp proxy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
```

and install it as a user CA on each device. A client that does not trust the CA, or that connects to a
name other than `RELAY_SITE_ADDRESS`, fails certificate validation by design.

Back up the `caddy-data` volume together with the relay data: it holds the CA key. Losing it means every
client must trust a new root certificate.

## Health checks

| Endpoint | Auth | Meaning |
|---|---|---|
| `GET /health` | none | liveness: the process is up and serving |
| `GET /ready` | none | readiness: local configuration and storage are usable; `200` when every check passes, `503` otherwise |

`/ready` runs only local probes and never makes a network call. Its checks are `settings` (required
values present), `storage` (configured directories exist and are writable), `auth` (at least one device
token), `models` (model names configured), `shutdown` (no shutdown in progress), and `limits`
(informational: sessions, runs, queued uploads).

| Symptom | Cause | Fix |
|---|---|---|
| `settings: missing required settings` | `HERMES_BASE_URL`, `INBOX_DIR`, `OUTBOX_DIR`, or device tokens unset | fill in `.env` |
| `storage: ... missing` or `not writable` | data volume not prepared, or `OUTBOX_DIR` absent | re-run the deploy; check `volume-init` |
| `auth: no device tokens configured` | neither the state file nor `DEVICE_TOKENS` has a token | issue a token, or set the bootstrap value |
| `models: missing model names` | `STT_MODEL` / `TTS_MODEL` / `VAD_MODEL` / `VOICE_BRAIN_MODEL` blank | set them in `.env` |

Compose:

```
docker compose ps
docker inspect --format '{{json .State.Health}}' hermes-talk-relay-1
docker compose logs --tail 100 relay
```

systemd:

```
systemctl status hermes-talk.service
curl -fsS http://127.0.0.1:8790/health
```

The relay writes its own log to `$DATA_DIR/logs/relay.log` and its audit trail to `$DATA_DIR/audit.log`
(see [Logging, audit, and retention](#logging-audit-and-retention)). `/ready` is cheap and safe to poll;
it never returns token material or absolute paths.

## Upgrades and rollback

The relay keeps no state outside `$DATA_DIR`: the inbox, the outbox, the logs, the audit trail, and the
device-token state file. An upgrade replaces the code; the data volume is untouched.

### Tag the image you deploy

```
RELAY_IMAGE=hermes-talk-relay:$(git rev-parse --short HEAD) \
  docker compose up -d --build
```

Record the tag you deployed and the tag you are replacing:

```
docker compose ps --format '{{.Image}}'
```

### Upgrade

1. Back up the token state file and `.env`:

   ```
   docker compose exec relay cat /var/lib/hermes-talk/device_tokens.json > device_tokens.backup.json
   ```

2. Check out the new revision and rebuild under a new tag:

   ```
   git -C .. fetch --tags && git -C .. checkout <new-tag>
   RELAY_IMAGE=hermes-talk-relay:<new-sha> docker compose up -d --build
   ```

3. Run the health gate from the previous section. `/ready` must be 200 before the upgrade is done.
4. Keep the previous image tag until the next upgrade; it is the rollback target.

### Roll back

Rollback is an image swap, not a data restore. The data volume stays mounted, so transcripts, artifacts,
and issued device tokens survive.

```
# 1. stop the failed release without deleting the data volume
docker compose stop relay proxy

# 2. redeploy the previous image tag (it is already built locally)
RELAY_IMAGE=hermes-talk-relay:<previous-sha> docker compose up -d --no-build

# 3. verify
docker compose ps
curl -fsS http://127.0.0.1:8790/health
curl -fsS http://127.0.0.1:8790/ready
```

`docker compose down` is **not** part of a rollback: it removes the containers but keeps the named
volumes; `docker compose down -v` deletes the data volume and must be deliberate.

For the systemd path, roll back the checkout instead:

```
sudo -u hermes-talk git -C /opt/hermes-talk checkout <previous-tag>
sudo -u hermes-talk /opt/hermes-talk/.venv/bin/pip install -e "/opt/hermes-talk/relay[speech]"
sudo systemctl restart hermes-talk.service      # operator go required
```

### Data compatibility

The device-token state file carries `version: 1`. A build that meets a higher state version refuses to
load it (`unsupported token state version`). Rolling back across a state-version bump therefore requires
restoring the backup taken in step 1 of the upgrade; otherwise the relay starts with no active tokens and
`/ready` reports `auth: no device tokens configured`.

### Rehearsal record

Rehearsed on 2026-11-01 from a clean copy of this repository: `git archive` of commit `1abc5d0` into
an empty directory (no `.git`, no working tree, no prior containers), with an `.env` derived from
`.env.example` and an empty model directory. Port 443 on the rehearsal host was held by another
service, so the site address was `voice.example.lan:8443` — the documented escape hatch — instead of
the default 443.

1. `docker compose up -d --build` with `RELAY_IMAGE=hermes-talk-relay:1abc5d0`: `volume-init` exited
   0, relay `healthy`, proxy `healthy`. Listeners were `127.0.0.1:8790` (relay) and `*:8443` (proxy)
   only. `/health` 200 and `/ready` 200 with every check green (`settings`, `storage`, `auth`,
   `models`, `shutdown`, `limits`).
2. TLS: `docker compose cp proxy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt`, then
   `curl --cacert ./caddy-root.crt --resolve voice.example.lan:8443:127.0.0.1
   https://voice.example.lan:8443/health` -> 200, certificate issued by `CN=Caddy Local Authority -
   ECC Intermediate` for `voice.example.lan`. `/v1/artifacts` returned 401 without a token and 200
   with the token issued through `docker compose exec relay python -m relay.tokens issue --label
   rehearsal-phone`.
3. Rollback: `docker compose stop relay proxy`, then
   `RELAY_IMAGE=hermes-talk-relay:e26f7e1 docker compose up -d --no-build` — the previous release came
   up `healthy` on the same data volume, `/ready` 200 reporting `1 active device token(s)`,
   `/v1/artifacts` 200 with the token issued before the rollback, and `audit.log`, `relay.log` and
   `device_tokens.json` still present.
4. Roll forward: `RELAY_IMAGE=hermes-talk-relay:1abc5d0 docker compose up -d --no-build` — `healthy`
   again, the same checks green.
5. `docker compose down` — containers and network removed, the three named volumes kept.

Two defects surfaced and were fixed in this commit:

- The image did not build at all: `relay/Dockerfile` never copied `transport/`, which
  `pyproject.toml` declares as a package, so `pip install .` failed with `package directory
  './transport' does not exist`. The previous release (`e26f7e1`) is affected, so the rollback
  rehearsal rebuilt its image from the same source with the current Dockerfile.
- `WORKDIR /app` put the repository's `logging/` package ahead of the standard library on `sys.path`
  for `python -c` and `python -m`, which broke the container healthcheck and `python -m
  relay.tokens`; `PYTHONSAFEPATH=1` fixes both. The image `CMD` also hard-coded `--host 0.0.0.0` and
  so ignored `RELAY_HOST`; it now runs `python -m relay.app`, which makes the loopback bind a
  configuration fact rather than an argument.

The rehearsal tore the stack down afterwards. Nothing on the host was restarted: the stack was
created, exercised, and removed.

## Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| relay container restarts immediately | `.env` missing or invalid; `RELAY_PORT` out of range | `docker compose logs relay` |
| `/ready` 503 with `storage: ... not writable` | data volume still owned by root | re-run `docker compose up -d`, confirm `volume-init` exited 0 |
| proxy returns 502 | relay not listening on `127.0.0.1:8790` | `curl -fsS http://127.0.0.1:8790/health` |
| proxy fails to start, `address already in use` | 80/443 taken by another service | put `:8443` in `RELAY_SITE_ADDRESS`, or free the port |
| client rejects the certificate | internal CA not installed, or wrong name | re-export `caddy-root.crt`; use the exact `RELAY_SITE_ADDRESS` |
| `401 unauthorized` from the phone | device token revoked, or not issued | `docker compose exec relay python -m relay.tokens list` |

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
make no network calls. `DATA_DIR` must be set or pass `--state-path`. In the compose stack, prefix them
with `docker compose exec relay`:

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

The relay accepts a device token only from a single `Authorization: Bearer *** header. Query
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
15 3 * * * hermes-talk sh -c 'set -a; . /etc/hermes-talk/relay.env; set +a; cd /opt/hermes-talk && .venv/bin/python -m relay.maintenance prune >> "${DATA_DIR:-/var/lib/hermes-talk}/logs/retention.log" 2>&1'
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
User=hermes-talk
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
curl -X DELETE -H "Authorization: Bearer ***" \
  http://127.0.0.1:8642/api/sessions/<session-id>
```

Manual deletions are permanent and are **not** written to the audit trail:
only the automated pruner appends `retention.*` entries.

`python -m relay.maintenance prune` is offline apart from the loopback Hermes
session call; inbox and outbox pruning touch only the local filesystem.
