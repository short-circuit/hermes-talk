# Operator runbook

Day-2 operations for a deployed Hermes Talk relay. This document assumes the host was installed with
either `deploy/compose.yaml` or `deploy/hermes-talk.service`; installation, topology, the port table,
upgrades, rollback, and the full retention reference are in [`DEPLOYMENT.md`](DEPLOYMENT.md).
Sections 12 and 13 cover the paired Android device: the assistant role, the battery exemption, boot
start, and `dumpsys` troubleshooting.

Use one deployment path at a time. The compose stack and the systemd unit both bind `127.0.0.1:8790`;
running both produces an address-in-use failure.

## Terms and conventions

| Term | Meaning |
|---|---|
| relay | This repository's Python service (`python -m relay.app`) |
| proxy | Caddy, the only LAN-facing listener; terminates TLS with its internal CA |
| gateway | The Hermes Agent gateway on the host; hosts the API server and messaging platforms |
| voice brain | The local llama-server that answers small talk and chooses tools |
| `$REPO` | Repository checkout; examples use `/opt/hermes-talk` |
| `$DATA_DIR` | Relay state root; examples use `/var/lib/hermes-talk` |
| `$BACKUP_DIR` | Backup destination outside the repository; examples use `$HOME/hermes-talk-backups` |

Host-side commands use the loopback defaults below. Set them for the session, or replace them with the
values in `.env` when the deployment uses different ports:

```
REPO=/opt/hermes-talk
DATA_DIR=/var/lib/hermes-talk
BACKUP_DIR="$HOME/hermes-talk-backups"
export HERMES_BASE_URL=http://127.0.0.1:8642
export VOICE_BRAIN_BASE_URL=http://127.0.0.1:8080
export VOICE_BRAIN_MODEL=lfm3-1.7b
```

Commands are shown for the compose path and, where it differs, for the systemd path. Run compose
commands from `$REPO/deploy`. Run systemd commands from `$REPO`.

**Approval rule.** Never restart a running service without the owner's explicit go. Say what will drop,
then wait for confirmation. Starting a stopped service or stopping one during a maintenance window is
announced, but only a restart needs the approval. In a headless worker, record the context in a card
comment and block with `needs_input` instead of restarting. A Caddy reload is not a restart; use it for
proxy configuration changes.

**Local-only invariant.** Every runtime endpoint is loopback or LAN. Do not add a cloud STT, TTS, or
LLM endpoint. Model downloads happen during installation, never during a call.

**First start.** On a fresh install, use this order: section 1 (prerequisites), section 2 (start),
section 5 (issue the first token), section 3 (health and TLS smoke), section 4 (warm the models),
section 12 (device setup: assistant role, battery exemption, boot start), then one call from the phone.
On an existing install, start at section 2.

## 1. Before the first start

Confirm these are in place. Missing items are configuration errors, not runtime failures.

- `.env` exists outside version control, mode `0600`. From a clean checkout the example lives at the
  repository root, so from `deploy/` use `cp ../.env.example .env`, not `cp .env.example .env`.
- `HERMES_BASE_URL` and `HERMES_API_KEY` point at the host's Hermes API server. The gateway must already
  be running; starting the relay does not start Hermes.
- `VOICE_BRAIN_BASE_URL` points at the loopback llama-server, and `VOICE_BRAIN_MODEL` names the model it
  serves.
- Model files exist at the configured paths: the sherpa STT directory (`tokens.txt`, `encoder-*.onnx`,
  `decoder-*.onnx`, `joiner-*.onnx`), the Kokoro TTS model and voices file, and the whisper directory
  when `STT_ENGINE=whisper`.
- `$DATA_DIR` is outside the repository and writable by the relay user. Compose creates it through the
  `volume-init` one-shot; the systemd unit creates it through `StateDirectory`, but the outbox is never
  created automatically:

  ```
  sudo install -d -o hermes-talk -g hermes-talk -m 0700 /var/lib/hermes-talk/outbox
  ```

- A device token exists, or `DEVICE_TOKENS` contains a bootstrap token for the first start. Issue real
  tokens before the first call and remove the bootstrap value afterwards.

`/ready` only checks configured names and local paths. It does not open model files and does not call
Hermes or the voice brain. A green `/ready` is necessary, not sufficient.

## 2. Start

### Compose

```
cd "$REPO/deploy"
docker compose up -d
docker compose ps
```

`up -d` runs `volume-init` first, then starts the relay and waits for its healthcheck before starting
the proxy. Add `--build` only when relay code changed; add `--no-build` when the image is pinned and
already present. Do not use `--pull always` on a host that must stay offline. If `up -d` recreates a
running service because its config or image changed, treat that as a restart and get the owner's go
first.

### systemd

```
sudo systemctl start hermes-talk.service
systemctl is-active hermes-talk.service
```

The unit starts the relay only. Start the proxy separately after the relay is healthy:

```
sudo systemctl start caddy
```

If the relay is already enabled, `systemctl enable --now hermes-talk.service` both enables and starts
it. `Restart=on-failure` restarts the relay after a crash; that automatic restart is not a planned
restart and needs no approval.

### Start order

1. Hermes gateway and its API server (leave them running if they already are).
2. Voice-brain llama-server, with the model loaded.
3. Relay.
4. Proxy.

The relay can start before Hermes or the voice brain; calls fail until both are reachable.

## 3. Verify the relay

```
curl -fsS http://127.0.0.1:8790/health
curl -fsS http://127.0.0.1:8790/ready
```

Expected: `/health` returns `{"status":"ok","service":"voice-relay","version":"..."}` with HTTP 200.
`/ready` returns `{"status":"ready","checks":[...]}` with HTTP 200 and every check `ok`. A 503 names
the failing check:

| Check | Meaning | First fix |
|---|---|---|
| `settings` | required environment values missing | fill `.env`, restart the relay |
| `storage` | inbox, outbox, or log root missing or not writable | create the directory, fix ownership |
| `auth` | no active token and no bootstrap token | issue a token (section 5) |
| `models` | a model name is empty | set `VOICE_BRAIN_MODEL`, `STT_MODEL`, `TTS_MODEL`, `VAD_MODEL` |
| `shutdown` | relay is stopping | wait for the process to exit, then start it |
| `limits` | reports active sessions, runs, and uploads | informational; it is always `ok` |

Check the upstreams separately:

```
curl -fsS "${HERMES_BASE_URL%/}/health"
curl -fsS "${VOICE_BRAIN_BASE_URL%/}/health"
```

The Hermes `/health` endpoint is unauthenticated. If either upstream fails, the relay is healthy but
calls will fail; fix the upstream, do not restart the relay as a reflex.

### TLS and authentication smoke test

Export the proxy's root certificate once per client, then test through the proxy. The site name comes
from `RELAY_SITE_ADDRESS`; the repository default is the placeholder `voice.example.lan`. On the
compose path:

```
cd "$REPO/deploy"
docker compose run --rm -T --no-deps --entrypoint cat proxy \
  /data/caddy/pki/authorities/local/root.crt > "$HOME/caddy-root.crt"
```

On the systemd path the Caddy data directory depends on the package; export the same
`pki/authorities/local/root.crt` file as described in `DEPLOYMENT.md` § "Reverse proxy and internal
TLS". Keep the certificate outside the repository.

```
SITE=voice.example.lan          # replace with your RELAY_SITE_ADDRESS
printf 'device token: '
read -r -s AUTH_TOKEN          # paste the device token; input is hidden
printf '\n'
curl -fsS --cacert "$HOME/caddy-root.crt" --resolve "$SITE:443:127.0.0.1" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" "https://$SITE/v1/whoami"
unset AUTH_TOKEN
```

If the site address carries a port (for example because 443 is taken), use that port in both
`--resolve` and the URL. Expected body:

```
{"label":"phone","token_id":"...","issued_at":"..."}
```

A `401` means the token is wrong or revoked; a certificate error means the client does not trust the
proxy CA. See `DEPLOYMENT.md` § "Reverse proxy and internal TLS" for exporting the root certificate.

On a fresh install with no token and no bootstrap value, `/ready` returns 503 with the `auth` check
failing. That is expected: issue a token in section 5 and re-check; the relay reloads the state file
without a restart.

Final acceptance is one short call from the paired phone: audio both ways, then hang up and confirm
the `limits` detail in `/ready` returns to zero active sessions.

## 4. Warm the models

The relay loads speech models lazily. `StreamingTts.warmup()` is the supported way to pay the TTS
cold-start cost before a call; the STT adapter loads its recognizer when it is constructed. The voice
brain is a separate llama-server: its model loads at server start, so send one trivial completion
after it reports healthy. Hermes loads its agent model on the first run.

### TTS and STT

Compose, from `$REPO/deploy`:

```
docker compose exec -T relay python - <<'PY'
import asyncio

from relay.config import Settings
from relay.speech.stt import create_stt
from relay.speech.tts import create_tts


async def main() -> None:
    settings = Settings(_env_file=None)
    stt = create_stt(settings)
    tts = create_tts(settings)
    try:
        elapsed_ms = await tts.warmup()
        print(f"stt={stt.name} tts_warmup_ms={elapsed_ms:.1f}")
    finally:
        await tts.close()


asyncio.run(main())
PY
```

Systemd, using the unit's environment file so the service user gets the configured model paths:

```
sudo systemd-run --wait --pipe \
  --property=User=hermes-talk \
  --property=WorkingDirectory=/opt/hermes-talk \
  --property=EnvironmentFile=/etc/hermes-talk/relay.env \
  /opt/hermes-talk/.venv/bin/python - <<'PY'
import asyncio

from relay.config import Settings
from relay.speech.stt import create_stt
from relay.speech.tts import create_tts


async def main() -> None:
    settings = Settings(_env_file=None)
    stt = create_stt(settings)
    tts = create_tts(settings)
    try:
        elapsed_ms = await tts.warmup()
        print(f"stt={stt.name} tts_warmup_ms={elapsed_ms:.1f}")
    finally:
        await tts.close()


asyncio.run(main())
PY
```

Expected output: `stt=sherpa tts_warmup_ms=<number>` (or `stt=whisper` when configured). The number is
host-specific; a few hundred milliseconds is normal. The relay does not call `warmup()` during startup,
so run this once after a relay restart and before the first call. An onnxruntime warning about failing
to persist a telemetry device id is harmless under the read-only root filesystem. The first call
without warm-up pays roughly the same cost again, which can exceed the first-audio target. A
`SpeechModelUnavailable` error means a model path is wrong or a file is missing; fix the path in `.env`
and restart the relay. `failed to map segment from shared object` means the container's `/tmp` is
mounted `noexec`; the relay needs `exec` there because phonemizer maps a shared library from a
temporary directory.

### Voice brain

```
curl -fsS "${VOICE_BRAIN_BASE_URL%/}/health"
curl -fsS "${VOICE_BRAIN_BASE_URL%/}/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"${VOICE_BRAIN_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":1}"
```

A successful completion means the model is resident. If the first real turn still hits
`VOICE_BRAIN_FIRST_TOKEN_TIMEOUT_S`, the server is loading or the model is too large for the available
VRAM; check the llama-server log before raising the timeout.

## 5. Device tokens

The token CLI writes hash-only state; the plaintext token is printed once. Run it as the same user that
runs the relay. A root-owned state file is mode `0600` and the relay cannot read it, so authentication
fails until the ownership is repaired.

### Compose

```
cd "$REPO/deploy"
docker compose exec relay python -m relay.tokens issue --label phone
docker compose exec relay python -m relay.tokens list
docker compose exec relay python -m relay.tokens rotate --label phone
docker compose exec relay python -m relay.tokens revoke --id <token-id>
```

### systemd

`DATA_DIR` is the only variable the token CLI needs; passing it explicitly avoids reading the
root-owned environment file:

```
sudo -u hermes-talk env DATA_DIR=/var/lib/hermes-talk \
  /opt/hermes-talk/.venv/bin/python -m relay.tokens list
```

Use the same prefix for `issue`, `rotate`, and `revoke`. Add `TOKEN_STATE_PATH=/path/to/device_tokens.json`
when the state file is not at `$DATA_DIR/device_tokens.json`.

### Issue

```
python -m relay.tokens issue --label phone
```

Output: the plaintext token on the first line (`ht_...`), then a summary with the label, the token id,
and the state path. Give the token to the device over a local channel; it is not recoverable from the
state file.

### Rotate

```
python -m relay.tokens rotate --label phone
```

Rotation writes the successor and revokes the predecessor in one atomic replace. The old token stops
working on the next request; update the phone immediately. No relay or gateway restart is required:
the relay re-stats the state file on every verification and reloads it when the file changes.

### Revoke

```
python -m relay.tokens list
python -m relay.tokens revoke --id <token-id>
```

`list` prints `token_id`, label, issue time, and `active` or `revoked`. Revoke the active id when a
device is lost. After a rotation, `revoke --label` targets the most recently issued active record for
that label, so it revokes the current token rather than the already-revoked predecessor. Revoking by
explicit `--id` from `list` stays the safest habit because it is unambiguous, and re-revoking an
already-revoked id is a no-op that exits 0.

Revocation is immediate and needs no restart. Audit entries `auth.issue`, `auth.rotate`, and
`auth.revoke` are appended to `$DATA_DIR/audit.log`; the plaintext token never appears there.

### Bootstrap tokens

`DEVICE_TOKENS` is consulted only while the state file has no active tokens. If it is still set and
you revoke the last real token, the bootstrap token becomes valid again. Remove `DEVICE_TOKENS` after
issuing real tokens.

## 6. Stop

### Compose

Stop the proxy first so no new call arrives, then drain the relay:

```
cd "$REPO/deploy"
docker compose stop proxy
docker compose stop relay
```

`docker compose stop` keeps the containers and volumes; `docker compose start` resumes them. For a
teardown that removes containers but keeps data, use `docker compose down`. Never use
`docker compose down -v` unless you intend to delete the inbox, outbox, logs, audit trail, token state,
and the proxy CA.

The relay has a 30-second stop grace period; an active call is closed and `service.stop` is audited.
Stop when idle if you can. A stop is not a restart, but it still interrupts live calls; announce it.

### systemd

```
sudo systemctl stop caddy
sudo systemctl stop hermes-talk.service
```

The unit has `TimeoutStopSec=30s`. `systemctl stop` suppresses `Restart=on-failure`; `systemctl restart`
is a planned restart and needs the owner's go.

## 7. Logs and audit

| What | Location | Notes |
|---|---|---|
| Relay application log | `$DATA_DIR/logs/relay.log` | JSON, redacted, rotates at `LOG_MAX_BYTES`, keeps `LOG_BACKUP_COUNT` |
| Audit trail | `$DATA_DIR/audit.log` | append-only JSONL, mode `0600`, rotates at `AUDIT_MAX_BYTES`, keeps `AUDIT_BACKUP_COUNT` |
| Audio debug recordings | `$DATA_DIR/recordings/` | only when `RECORD_AUDIO=1`; raw audio, watch disk use |
| Compose stdout | `docker compose logs -f --tail=200 relay proxy` | container stdout; proxy access log is JSON |
| systemd stdout | `journalctl -u hermes-talk -f`, `journalctl -u caddy -f` | includes start/stop and proxy access records |
| Retention job output | `$DATA_DIR/logs/retention.log` | only if the cron variant redirects there |

Read the files directly on the systemd path:

```
sudo tail -n 100 /var/lib/hermes-talk/logs/relay.log
sudo tail -n 100 /var/lib/hermes-talk/audit.log
```

On the compose path the data volume is not a host path; read it through the container:

```
docker compose exec relay tail -n 100 /var/lib/hermes-talk/logs/relay.log
docker compose exec relay tail -n 100 /var/lib/hermes-talk/audit.log
```

Useful audit queries (JSON lines, one event per line):

```
grep '"event": "auth.' "$DATA_DIR/audit.log" | tail -n 20
grep '"event": "retention.error"' "$DATA_DIR/audit.log" | tail -n 20
grep '"event": "retention.summary"' "$DATA_DIR/audit.log" | tail -n 5
```

Do not truncate or edit `audit.log`; rotation and pruning own it. The relay writes no file sink when
`DATA_DIR` is unset or resolves inside the repository, so a missing `relay.log` is usually a path
problem, not a logging bug.

### Log pruning

`make prune-logs` prunes `$DATA_DIR/logs` and `$DATA_DIR/audit.log` by the configured age and size.
It does not touch inbox, outbox, or transcripts. It reads the log limits from the environment, so run
it with the service environment when `LOG_*` or `AUDIT_*` values were customized.

Compose path:

```
cd "$REPO/deploy"
docker compose exec relay python -m relay.logging.prune --dry-run
```

Systemd path:

```
sudo systemd-run --wait --pipe \
  --property=User=hermes-talk \
  --property=WorkingDirectory=/opt/hermes-talk \
  --property=EnvironmentFile=/etc/hermes-talk/relay.env \
  /opt/hermes-talk/.venv/bin/python -m relay.logging.prune --dry-run
```

Remove `--dry-run` to prune. `make prune-logs` wraps the same module; it is equivalent only when the
relay environment is already loaded. Details and the retention schedule are in `DEPLOYMENT.md` §
"Logging, audit, and retention".

## 8. Data retention (operator summary)

The full policy, windows, and scheduling examples are in `DEPLOYMENT.md` § "Data retention and
deletion". The operator-relevant facts:

- `INBOX_RETENTION_DAYS`, `OUTBOX_RETENTION_DAYS`, and `TRANSCRIPT_RETENTION_DAYS` are inert by default;
  `0` keeps files until manual deletion. Changing them requires an env edit and a relay restart, or a
  per-run CLI override. A relay restart needs the owner's go.
- Manual run: on compose, `docker compose exec relay python -m relay.maintenance prune --dry-run`.
  On systemd, use the installed oneshot (`sudo systemctl start hermes-talk-retention.service`) or an
  ad-hoc transient unit that reads the environment file:

  ```
  sudo systemd-run --wait --pipe \
    --property=User=hermes-talk \
    --property=WorkingDirectory=/opt/hermes-talk \
    --property=EnvironmentFile=/etc/hermes-talk/relay.env \
    /opt/hermes-talk/.venv/bin/python -m relay.maintenance prune --dry-run
  ```

  Run from the repository root, never from inside `relay/` (the `logging` directory there shadows the
  stdlib module); in the container image use `-w /`.
- Scheduled run: prefer the systemd `.service` + `.timer` in `DEPLOYMENT.md`; systemd reads
  `EnvironmentFile` as root. The cron example runs as `hermes-talk` and must be able to read
  `/etc/hermes-talk/relay.env`; if the file is `0600 root:root`, either install it `0640 root:hermes-talk`
  or use the timer.
- Deletions append `retention.deleted`, `retention.skipped`, `retention.error`, and `retention.summary`
  to `$DATA_DIR/audit.log` with a relative path or session id, age, and reason. They never contain file
  contents or transcript text.
- A file claimed by a live turn is skipped and retried next run; a busy Hermes session returns
  `409 session_active_turn` and is skipped. Only sessions with `source=api_server` are eligible;
  Telegram, CLI, and Kanban sessions are left alone.
- Exit code `0` means clean; `1` means at least one error was recorded (for example Hermes unreachable
  or `HERMES_API_KEY` missing). Read the JSON report or the audit entry before retrying.
- Manual deletion: `rm` inside `$INBOX_DIR` or `$OUTBOX_DIR` while the relay is idle. Delete a
  transcript only through `DELETE /api/sessions/{id}` with the API key, never by editing the Hermes
  database. Manual deletions are not audited.

## 9. Backup and restore

### What to back up

| Item | Where | Why |
|---|---|---|
| Inbox | `$DATA_DIR/inbox/` | attachments received from devices |
| Outbox | `$DATA_DIR/outbox/` | artifacts waiting for download |
| Token state | `$DATA_DIR/device_tokens.json` (or `TOKEN_STATE_PATH`) | restores every device token; hashes only |
| Audit trail | `$DATA_DIR/audit.log` | approvals, denials, retention, auth events |
| Relay logs | `$DATA_DIR/logs/` | optional; useful for an incident |
| Environment | `deploy/.env` or `/etc/hermes-talk/relay.env` | secrets and host paths; store securely |
| Proxy CA data | `caddy-data` volume or Caddy's data directory | losing it invalidates every trusted client |

Transcripts are not in `$DATA_DIR`; they live in Hermes and are managed there. A relay backup restores
the relay's files, not the Hermes session database.

### Quiesce

Take a consistent backup while no call is active. Stop the proxy first, then the relay, or wait until
`/ready` reports `sessions 0/N` and no uploads are in flight. The pruner skips live turns, but a raw
file copy does not.

```
cd "$REPO/deploy"
docker compose stop proxy
docker compose stop relay
```

### Compose backup

Use a one-off container from the relay image, run as the data owner, and stream the archive to the
host so the archive is owned by the operator, not by uid 10001:

```
install -d -m 0700 "$BACKUP_DIR"
cd "$REPO/deploy"
docker compose run --rm -T --no-deps --user 10001:10001 --entrypoint tar relay \
  czf - -C /var/lib/hermes-talk . > "$BACKUP_DIR/relay-data-$(date +%F).tar.gz"

docker compose run --rm -T --no-deps --entrypoint tar proxy \
  czf - -C /data . > "$BACKUP_DIR/caddy-data-$(date +%F).tar.gz"

install -m 0600 .env "$BACKUP_DIR/env-$(date +%F)"
```

The relay service drops all capabilities, so `--user 0:0` cannot read a `0700` volume owned by uid
10001; running as 10001 is deliberate. The proxy image has `tar` and reads its own root-owned data
volume. The Caddy archive contains the CA private key; protect it.

If `TOKEN_STATE_PATH`, `INBOX_DIR`, or `OUTBOX_DIR` points outside `$DATA_DIR`, archive that path
separately; the command above covers the compose defaults only.

Restart in the documented order:

```
docker compose up -d
```

### systemd backup

```
sudo systemctl stop hermes-talk.service
sudo install -d -m 0700 /var/backups/hermes-talk
sudo tar czf "/var/backups/hermes-talk/relay-data-$(date +%F).tar.gz" -C /var/lib/hermes-talk .
sudo tar czf "/var/backups/hermes-talk/relay-env-$(date +%F).tar.gz" -C /etc/hermes-talk relay.env
sudo systemctl start hermes-talk.service
```

Back up the Caddy data directory separately; the path depends on the Caddy package. It contains the
internal CA and must be restored with the proxy stopped.

### Validate

```
tar -tzf "$BACKUP_DIR/relay-data-$(date +%F).tar.gz" | head -n 20
```

The listing must include `inbox/`, `outbox/`, `audit.log`, and `device_tokens.json`. A backup that
opens but lacks the token state cannot restore authentication. Keep backups on a different disk or
host, and prune old copies deliberately; do not let them fill the data disk.

### Restore

Stop the proxy and relay first. Restore the data set as a unit; mixing an old audit log with a newer
token state loses events. Extraction merges into the existing directory, so files created after the
backup remain unless you move the current data aside first. On compose:

```
cd "$REPO/deploy"
docker compose stop proxy relay
docker compose run --rm -T --no-deps --user 10001:10001 --entrypoint tar relay \
  xzf - -C /var/lib/hermes-talk < "$BACKUP_DIR/relay-data-YYYY-MM-DD.tar.gz"
docker compose run --rm -T --no-deps --entrypoint sh proxy \
  -c 'tar xzf - -C /data' < "$BACKUP_DIR/caddy-data-YYYY-MM-DD.tar.gz"
docker compose up -d
```

`volume-init` runs again on `up` and repairs ownership. On systemd:

```
sudo systemctl stop hermes-talk.service
sudo tar xzf "/var/backups/hermes-talk/relay-data-YYYY-MM-DD.tar.gz" -C /var/lib/hermes-talk
sudo chown -R hermes-talk:hermes-talk /var/lib/hermes-talk
sudo systemctl start hermes-talk.service
```

After restore, verify `/health`, `/ready`, `python -m relay.tokens list`, and one authenticated
`/v1/whoami`. If the backup predates a relay upgrade, check the token-state version note in
`DEPLOYMENT.md` § "Upgrades and rollback" before starting the relay.

## 10. Common failures and fixes

Start every incident with the same three observations: process state, `/ready` detail, and the last
100 log lines. Do not restart the gateway to fix a relay problem.

| Symptom | Likely cause | Fix |
|---|---|---|
| `env file .../.env not found` | `.env` missing from `deploy/` | `cp ../.env.example .env`, edit, `chmod 600` |
| relay exits immediately | invalid or missing required setting | `docker compose logs relay`; fix `.env`; restart relay |
| `address already in use` on 8790 | compose stack and systemd unit both running | stop one path; confirm with `ss -ltnp` |
| `/ready` 503 `settings` | required value missing | fill `.env`; restart relay |
| `/ready` 503 `storage` | inbox/outbox/log root missing or not writable | create the directory; fix ownership |
| `/ready` 503 `auth` | no active token, no bootstrap token | issue a token; do not restart |
| `/ready` 503 `models` | empty model name | set the four model names; restart relay |
| `SpeechModelUnavailable` on first call | model path wrong or file missing | check the path and filenames; restart relay |
| TTS `failed to map segment from shared object` | container `/tmp` mounted `noexec` | use the current `compose.yaml` (`/tmp` has `exec`), or add `exec` to the tmpfs |
| `401 invalid_token` from a device | token revoked, mistyped, or from another state file | `list`; issue/rotate; update device |
| token CLI `PermissionError` on `$DATA_DIR` | CLI run as the wrong user | run as the relay user; fix ownership |
| token CLI `DATA_DIR is unset` | environment not exported | pass `DATA_DIR` or `--state-path` |
| token state owned by root | CLI was run with `sudo` | `chown hermes-talk:hermes-talk` the state, audit, and logs |
| `502` from the proxy | relay down or not listening | `curl 127.0.0.1:8790/health`; start relay |
| TLS `unknown authority` | client lacks the proxy root CA | export and install the root certificate |
| `x509` name mismatch | site name differs from `RELAY_SITE_ADDRESS` | use the configured name or fix the hosts entry |
| voice-brain first-token timeout | model loading, VRAM pressure, or wrong base URL | warm the model; check llama-server log; verify URL |
| Hermes `401` | `HERMES_API_KEY` mismatch | fix the relay key and restart relay; restart gateway only if its key changed (ask) |
| Hermes unreachable | gateway or API server down | `hermes gateway status`; start if stopped; ask before restarting |
| `409 session_active_turn` in pruner | session busy | expected skip; retry next run |
| pruner exit `1` | Hermes unreachable or key missing | read the report/audit; fix upstream; retry |
| `logging` import shadowed | command run inside `relay/` | run from `$REPO`; in container use `-w /` |
| cron retention permission denied | cron user cannot read `0600` env file | use the systemd timer or make the file `0640 root:hermes-talk` |
| disk full | recordings, logs, backups, or attachments | `df -h`; `du -sh $DATA_DIR/*`; prune and archive |
| relay crash-looping | bad config or model path | `systemctl stop`, fix, `systemctl start`; do not restart-loop |
| proxy config error | invalid Caddyfile | `caddy validate`; fix; `caddy reload` |
| `volume-init` fails | image missing or volume permissions | build the image; inspect `docker compose logs volume-init` |

## 11. The no-restart rule for the Hermes gateway

The gateway is not the relay. It hosts the API server and every messaging platform. Restarting it
terminates in-flight agent runs, drops open SSE streams, and interrupts messaging sessions; the relay
sees failed runs and the active call may need to be repeated. A gateway restart is therefore a
planned change, not a troubleshooting reflex.

- Check state with `hermes gateway status`. Do not restart it to pick up a relay `.env` change, a token
  rotation, or a Caddyfile edit; none of those need it.
- Rotate and revoke tokens live; the relay reloads the state file on the next request.
- Change relay limits, retention, model paths, or the Hermes key in `.env`; restart the relay, not the
  gateway.
- Change the Caddyfile; validate and reload Caddy. A reload keeps listeners and connections.
- Enable or reconfigure the Hermes API server (`gateway.platforms.api_server.enabled`, `API_SERVER_*`);
  this is the change that does require a gateway restart. Stage the edit, tell the owner what will
  drop, and wait for an explicit go. Exact commands, the observed settings and a smoke test are in
  [`ops/api-server.md`](ops/api-server.md).
- Headless: leave a card comment with the pending change and block with `needs_input`; do not restart.

After an approved restart:

```
hermes gateway status
curl -fsS http://127.0.0.1:8642/health
curl -fsS http://127.0.0.1:8790/ready
```

Then make one short call from the phone. The relay does not need a restart after the gateway returns;
its next run opens a fresh connection.

| Change | Action | Restart? | Ask first? |
|---|---|---|---|
| Issue, rotate, or revoke a device token | token CLI | none | no |
| Relay limits, retention, models, Hermes key | edit `.env`, restart relay | relay | yes |
| Caddyfile | validate, reload Caddy | none | no |
| Hermes API server enable/config | edit Hermes config/env, restart gateway | gateway | yes |
| New relay image | build/up or systemctl restart | relay | yes |
| Host reboot | all services | gateway and relay | yes |

## 12. Android device: assistant role, battery exemption and boot start

These steps are device-side; the relay does not need them. They make the assistant reachable from the
power-button gesture and keep it alive across screen-off, task swipe and reboot. The reference platform
is the API 34 `hermes_talk_34` AVD (x86_64, Google APIs); API-level differences are called out where
they matter. Commands below use the default serial — add `-s <serial>` when more than one device is
attached.

Prerequisites:

```sh
adb devices                                             # <serial>  device
adb shell getprop ro.build.version.sdk                  # 34 on the reference AVD
adb shell pm list packages com.example.hermestalk       # package:com.example.hermestalk
adb shell pm grant com.example.hermestalk android.permission.RECORD_AUDIO
adb shell pm grant com.example.hermestalk android.permission.POST_NOTIFICATIONS
adb shell pm grant com.example.hermestalk android.permission.BLUETOOTH_CONNECT
```

`pm grant` is silent on success and idempotent, so it is safe to re-run after every install.

### 12.1 Default assistant role

The app declares an `ACTION_ASSIST` entry point. Two ways to make it the default assistant:

- **UI.** System Settings → Apps → Default apps → Digital assistant app → Hermes Talk. The in-app
  **Set as default assistant** button on the Settings screen opens the system dialog instead. On
  images that mark `android.app.role.ASSISTANT` as `requestable=false` (the Google-APIs API 34 image
  does), the request activity logs `Role is not requestable` and returns `RESULT_CANCELED`; use the
  shell grant below.
- **Shell.**

```sh
adb shell cmd role add-role-holder android.app.role.ASSISTANT com.example.hermestalk
adb shell cmd role get-role-holders android.app.role.ASSISTANT
adb shell settings get secure assistant
```

Expected: the first command exits 0 with no output; the second prints `com.example.hermestalk`; the
third prints `com.example.hermestalk/.MainActivity`.

Verify the gesture (219 is `KEYCODE_ASSIST`):

```sh
adb shell input keyevent 219
adb shell dumpsys activity activities | grep -E 'topResumedActivity'
```

Expected: `topResumedActivity=ActivityRecord{… com.example.hermestalk/.MainActivity …}`.

Verify the not-held path and the explicit entry point:

```sh
adb shell cmd role remove-role-holder android.app.role.ASSISTANT com.example.hermestalk
adb shell cmd role get-role-holders android.app.role.ASSISTANT   # the image's preinstalled assistant
adb shell am start -a android.intent.action.ASSIST -n com.example.hermestalk/.MainActivity
```

After a revoke the gesture follows the new holder; on the reference image that is
`com.google.android.googlequicksearchbox`. The explicit intent still opens Hermes Talk.

| Command | Observed on `hermes_talk_34` (API 34) |
|---|---|
| `cmd role add-role-holder … com.example.hermestalk` | exit 0; `get-role-holders` lists `com.example.hermestalk`; `settings get secure assistant` = `com.example.hermestalk/.MainActivity` |
| `input keyevent 219` | `MainActivity` becomes `topResumedActivity` |
| `cmd role remove-role-holder …` | holder becomes `com.google.android.googlequicksearchbox`; the gesture opens that app |
| in-app request button on this image | permission controller logs `Role is not requestable: android.app.role.ASSISTANT`, returns `RESULT_CANCELED`, the card stays `NotHeld` |

Two persistence rules matter:

- **Uninstalling the app drops the role.** A reinstall (including the APKs a `connectedAndroidTest`
  run installs and then removes) clears the holder; re-grant it afterwards.
- **The grant needs a moment to settle.** A reboot issued immediately after `add-role-holder` can lose
  it and the platform falls back to the preinstalled assistant. Wait a few seconds, confirm with
  `get-role-holders`, and re-check after any reboot.

### 12.2 Battery exemption

Without the exemption Doze can freeze the process in deep idle. The app shows **Allow background
activity** on the Settings screen when it is not exempt; that button opens the system exemption dialog
(system Settings → Apps → Special app access → Battery optimisation → Hermes Talk → Don't optimise).
From a development machine:

```sh
adb shell dumpsys deviceidle whitelist +com.example.hermestalk   # Added: com.example.hermestalk
adb shell dumpsys deviceidle whitelist | grep hermestalk         # user,com.example.hermestalk,<uid>
adb shell dumpsys deviceidle whitelist -com.example.hermestalk   # Removed: com.example.hermestalk
```

Reinstalling the app changes its uid and drops the whitelist entry, so re-check it after every install.

### 12.3 Boot start

1. Enable the assistant: app Settings → **Background assistant** → **Keep assistant running**. The
   status line reads `Running (microphone)` while the app is visible. This is the opt-in the boot
   receiver reads; the flag is the plain preference `hermes_talk_persistent` /
   `assistant_enabled`, default off. Inspect it with:

```sh
adb shell run-as com.example.hermestalk cat shared_prefs/hermes_talk_persistent.xml
```

2. Confirm the service state before rebooting:

```sh
adb shell dumpsys activity services com.example.hermestalk \
  | grep -E 'isForeground|startCommandResult|createdFromFg|types='
```

Expected while the app is visible: `isForeground=true … types=00000080` (microphone),
`createdFromFg=true`, `startCommandResult=1` (START_STICKY).

3. Reboot and verify the boot path:

```sh
adb reboot
# wait for sys.boot_completed=1; on the archive AVD: scripts/emulator-archive.sh wait-for-boot
adb shell dumpsys activity services com.example.hermestalk \
  | grep -E 'isForeground|startCommandResult|createdFromFg|allowStartForeground|mAllowWhileInUse|types='
adb logcat -d -v brief | grep -E 'PersistentAssistantBoot|PersistentAssistant:'
```

Expected: `types=40000000` (specialUse), `createdFromFg=false`, `startCommandResult=1`,
`mAllowWhileInUsePermissionInFgsReason=DENIED`; `allowStartForeground` is `BOOT_COMPLETED` or
`SYSTEM_ALLOW_LISTED` depending on how the boot allow-list resolves — do not rely on that field alone.
Logcat shows:

```
I/PersistentAssistantBoot: android.intent.action.BOOT_COMPLETED received; persistent assistant start requested
W ActivityManager: Foreground service started from background can not have location/camera/microphone access: service com.example.hermestalk/.call.PersistentAssistantService
W PersistentAssistant: Microphone foreground type refused (java.lang.SecurityException: … falling back
```

This is the documented API 34 rule, not a defect: a microphone foreground service cannot start from
`BOOT_COMPLETED`. The service retries with `specialUse` inside the same `onStartCommand`, so it is
alive after boot — but wake-word capture is paused because capture requires the microphone type. The
app promotes the service to `microphone` on resume:

```sh
adb shell am start -n com.example.hermestalk/.MainActivity
adb shell dumpsys activity services com.example.hermestalk | grep -E 'mAllowWhileInUse|types='
```

Expected: `mAllowWhileInUsePermissionInFgsReason=PROC_STATE_TOP` and `types=00000080`.

4. Verify the sticky restart:

```sh
pid=$(adb shell pidof com.example.hermestalk | tr -d '\r')
adb shell run-as com.example.hermestalk kill -9 "$pid"
sleep 5
adb shell pidof com.example.hermestalk   # a new pid
adb shell dumpsys activity services com.example.hermestalk | grep -E 'isForeground|startCommandResult'
```

Toggling **Keep assistant running** stops and starts the assistant service; it does not touch the
relay. If a call is live the call owns the microphone and is unaffected, but say so before toggling.

### 12.4 Order for a new device

1. Install the APK and grant `RECORD_AUDIO`, `POST_NOTIFICATIONS`, `BLUETOOTH_CONNECT`.
2. Grant the assistant role and verify the gesture (12.1).
3. Whitelist battery optimisation (12.2).
4. Enable **Keep assistant running** and confirm `Running (microphone)` (12.3).
5. Reboot once and confirm the service returns with `types=40000000`, then open the app and confirm the
   promotion to `types=00000080`.
6. Issue the device token (section 5), enter the relay URL and token in the app, and place one call.

## 13. Device troubleshooting with dumpsys

Read the state before changing anything. `dumpsys activity services` is the authoritative view of the
service the platform actually started:

| Field | Meaning | Healthy value |
|---|---|---|
| `isForeground` / `types=` | foreground service type held | `00000080` (microphone) while visible; `40000000` (specialUse) after a background start |
| `startCommandResult=1` | `START_STICKY` | 1 |
| `createdFromFg` | started from a visible context | `true` from the app, `false` from `BOOT_COMPLETED` |
| `allowStartForeground` | why the platform allowed the start | `BOOT_COMPLETED`, `PROC_STATE_TOP` or `SYSTEM_ALLOW_LISTED` |
| `mAllowWhileInUsePermissionInFgsReason` | microphone-type eligibility | `PROC_STATE_TOP` when promoted; `DENIED` in the background |

Supporting commands:

```sh
adb shell dumpsys power | grep -A2 'Wake Locks'          # size=0 unless wake-word capture is listening
adb shell dumpsys deviceidle | grep -E 'mState=|mLightState='   # ACTIVE; IDLE only while forced or dozing
adb shell dumpsys deviceidle whitelist | grep hermestalk        # user,com.example.hermestalk,<uid>
adb shell cmd role get-role-holders android.app.role.ASSISTANT
adb shell settings get secure assistant
adb shell logcat -d -v brief | grep -E 'PersistentAssistant|AssistantWakeWord|ActivityManager: Foreground'
```

Symptoms and fixes:

| Symptom | Likely cause | Action |
|---|---|---|
| Service missing from `dumpsys activity services` | toggle off, app not started since boot, or boot receiver filtered out | enable **Keep assistant running**, launch the app, re-check after a reboot |
| `types=40000000` right after boot | expected API 34 microphone refusal | open the app; the resume path promotes to microphone |
| `types=40000000` while the app is visible | promotion refused — `RECORD_AUDIO` revoked or the app is not top | re-grant `RECORD_AUDIO`, bring the app to the foreground |
| `Wake Locks: size=1` (or more) with `listening=false` | wake-lock leak | the status line should read `Running`, not `Listening`; the instrumentation suite asserts the lock is released. If it stays held, file a card |
| Service gone after swiping the task away | `stopWithTask` is `true` in the merged manifest | confirm `stopWithTask="false"`; reinstall if the manifest is stale |
| Service disappears after Doze | app not battery-exempt | whitelist it (12.2) |
| `Role is not requestable` in logcat | image marks the role non-requestable | shell grant (12.1) |
| `keyevent 219` opens another app | another package holds the role | `get-role-holders`; re-grant and wait before rebooting |
| Role reverts after reboot | grant not persisted, or the app was reinstalled | re-grant, confirm, then reboot |
| Wake word never fires | toggle off, `RECORD_AUDIO` missing, or the service holds `specialUse` | enable the toggle, grant the permission, open the app to promote |
| Detection does not open the call screen | background-activity launch refused; `AssistantLaunchResult.started=false` | use the notification's content intent; the failure is recorded with its message |

## 14. Handover checklist

- [ ] Relay is active and `/health` and `/ready` return 200.
- [ ] Hermes and voice-brain `/health` return 200.
- [ ] `python -m relay.tokens list` shows the expected active ids and no unexpected active token.
- [ ] TTS/STT warm-up printed a nonzero duration.
- [ ] An authenticated `/v1/whoami` through the proxy returns the expected label.
- [ ] `$DATA_DIR/logs/relay.log` and `$DATA_DIR/audit.log` are writable by the relay user.
- [ ] A backup younger than the agreed interval exists, opens, and contains `device_tokens.json`.
- [ ] No `retention.error` in the audit trail since the last run.
- [ ] The deployed image tag, `.env` location and mode, site address, and backup destination are
      recorded in the handover note.
- [ ] On the paired device: the assistant role is held, `KEYCODE_ASSIST` opens the call screen, and
      battery optimisation is whitelisted (section 12).
- [ ] The persistent assistant is either enabled and confirmed back after a reboot, or deliberately
      off — record which.

## References

- [`DEPLOYMENT.md`](DEPLOYMENT.md) — topology, ports, installation, health semantics, TLS, upgrades,
  rollback, logging, and the full data-retention policy.
- [`PRIVACY.md`](PRIVACY.md) — repository hygiene rules and remediation.
- [`security/threat-model.md`](security/threat-model.md) — token handling and trust boundaries.
- [`research/hermes-api.md`](research/hermes-api.md) — Hermes API contract used by the relay.
- [`ops/api-server.md`](ops/api-server.md) — enabling the Hermes API server and the smoke test that proves it.
- `.env.example` — every setting, including the conservative retention values.
