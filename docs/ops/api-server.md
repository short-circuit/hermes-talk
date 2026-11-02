# Hermes API server — enablement and smoke test

> Status: **enabled and verified** against a live gateway on 2026-11-02 (Hermes Agent `0.22.3`,
> `/health` reports `"version": "0.22.3"`). Every block below is a command that was run on the host
> plus the output it produced. The bearer token never appears: the commands show `$HERMES_API_KEY`
> and the recorded transcript prints it as `***`.
>
> Scope: the loopback endpoint the relay drives (see [`research/hermes-api.md`](../research/hermes-api.md)
> for the full contract). Nothing here leaves the host: the API server is bound to loopback and the
> model provider is local — no cloud LLM, STT or TTS is involved.

## 1. Required state

| Setting | Location | Value observed | Why |
|---|---|---|---|
| `platforms.api_server.enabled` | `~/.hermes/config.yaml` | `true` | starts the adapter; the CLI also resolves it as `gateway.platforms.api_server.enabled` |
| `API_SERVER_ENABLED` | `~/.hermes/.env` | `true` | platform switch read by the gateway |
| `API_SERVER_HOST` | `~/.hermes/.env` | `127.0.0.1` | loopback only — never `0.0.0.0` |
| `API_SERVER_PORT` | `~/.hermes/.env` | `8642` | the port the relay's `HERMES_BASE_URL` points at |
| `API_SERVER_KEY` | `~/.hermes/.env` | set (43 chars) | bearer token; must be ≥ 16 chars or the server refuses to start |

`API_SERVER_KEY` is a secret: it lives in `.env`, never in `config.yaml`, and never in this repository.
`API_SERVER_MODEL_NAME`, `API_SERVER_CORS_ORIGINS`, `API_SERVER_HEARTBEAT_SECONDS` and
`API_SERVER_LATENCY_SAMPLE_LIMIT` are supported but left at their defaults (no CORS: the relay is a
same-host server-side client).

On a host that does not have it yet, the whole enablement is:

```
# config.yaml — settings
hermes config set platforms.api_server.enabled true

# .env — platform switch and secret
API_SERVER_ENABLED=true
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=8642
API_SERVER_KEY=$(openssl rand -hex 32)
```

Then restart the gateway — **with the owner's go** (see §2).

## 2. Restart policy

Enabling or reconfiguring the API server is the one Hermes change that requires a gateway restart
([`RUNBOOK.md` §11](../RUNBOOK.md#11-the-no-restart-rule-for-the-hermes-gateway)): the listener is
created at boot. On this host that restart was **not needed and not performed** — the adapter was
already running before this change was verified:

```
$ hermes gateway status
● hermes-gateway.service - Hermes Agent Gateway - Messaging Platform Integration
     Loaded: loaded (/etc/systemd/system/hermes-gateway.service; enabled; preset: disabled)
     Active: active (running) since Mon 2026-11-02 02:54:06 CET; 9h ago
   Main PID: 3079477 (hermes)

$ stat -c '%n mtime=%y' ~/.hermes/config.yaml ~/.hermes/.env
~/.hermes/config.yaml mtime=2026-11-02 12:15:14
~/.hermes/.env mtime=2026-10-31 15:40:45

$ ss -ltnp | grep 8642
LISTEN 0      128      127.0.0.1:8642      0.0.0.0:*    users:(("hermes",pid=3079477,fd=143))
```

The listener belongs to the gateway's main PID and predates the last config edit, so the running
process is already serving the current settings. A restart would only drop in-flight runs and open
SSE streams for nothing. `hermes gateway status` does print a warning that the installed service
definition is outdated (`sudo hermes gateway restart --system` would refresh it); that is a separate,
optional change and was not applied here.

## 3. Smoke test

```
$ scripts/smoke_hermes_api.sh 2>&1 | tee /tmp/hermes-api-smoke.txt
```

The script reads the key from `$HERMES_API_KEY` or `~/.hermes/.env`, never prints it, creates one
`api_server` session for the turn and deletes it afterwards (`HERMES_SMOKE_KEEP=1` keeps it). It exits
non-zero if any check fails. Recorded run, 27.8 s end to end:

### 3.1 Listener and unauthenticated health

```
$ ss -ltn | grep 8642
LISTEN 0      128                                 127.0.0.1:8642       0.0.0.0:*

$ curl -sS -w '\nHTTP %{http_code}\n' http://127.0.0.1:8642/health
{"status": "ok", "platform": "hermes-agent", "version": "0.22.3"}
HTTP 200
PASS  GET /health -> 200
PASS  /health reports status ok
```

### 3.2 Bearer auth is enforced

```
$ curl -sS -w '\nHTTP %{http_code}\n' http://127.0.0.1:8642/v1/capabilities   # no Authorization header
{"error": {"message": "Invalid gateway API key (API_SERVER_KEY)", "type": "gateway_auth_error", "code": "gateway_auth_failed"}}
HTTP 401
PASS  GET /v1/capabilities without a key -> 401
```

### 3.3 Capabilities

```
$ curl -sS -H 'Authorization: Bearer $HERMES_API_KEY' http://127.0.0.1:8642/v1/capabilities
HTTP 200
PASS  GET /v1/capabilities -> 200

required feature flags:
run_submission = true
runs_idempotency = {"supported":true,"durable":true,"retention_seconds":86400}
run_status = true
run_events_sse = true
run_stop = true
run_steer = true
run_approval_response = true
tool_progress_events = true
approval_events = true
session_resources = true
session_chat = true
session_fork = true
session_model_lock = true
session_continuity_header = "X-Hermes-Session-Id"
session_key_header = "X-Hermes-Session-Key"
cors = false

PASS  every gated feature flag is true (missing: none)
PASS  features.cors is false (loopback-only client)
PASS  features.audio_api is false (speech stays in the relay)
```

### 3.4 Admission of a tool-using run

```
$ curl -sS -i -X POST -H 'Authorization: Bearer $HERMES_API_KEY' -H 'Content-Type: application/json' \
    -H 'X-Hermes-Session-Key: relay-smoke-1793619552-1017437' -H 'Idempotency-Key: smoke-1793619552-1017437' \
    -d '{"input":"Use your terminal tool to run the shell command `uname -s`, then reply with only the output."}' \
    http://127.0.0.1:8642/v1/runs

HTTP 202
{"run_id": "run_e405932666ab4dea82f9e666c333be78", "status": "started", "replayed": false}
X-Hermes-Session-Key: relay-smoke-1793619552-1017437
PASS  POST /v1/runs -> run_id present
```

### 3.5 Event stream (SSE)

```
$ curl -sS -N --max-time 180 -H 'Authorization: Bearer $HERMES_API_KEY' \
    http://127.0.0.1:8642/v1/runs/run_e405932666ab4dea82f9e666c333be78/events
: open

: keepalive

: keepalive

id: 0
data: {"event": "tool.started", "run_id": "run_e405932666ab4dea82f9e666c333be78", "timestamp": 1793619577.5755148, "tool": "terminal", "preview": "uname -s", "tool_call_id": "call_b2cb219bc5c6ab5f21dc17e0", "seq": 0}

id: 1
data: {"event": "tool.completed", "run_id": "run_e405932666ab4dea82f9e666c333be78", "timestamp": 1793619577.6971946, "tool": "terminal", "duration": 0, "error": false, "tool_call_id": "call_b2cb219bc5c6ab5f21dc17e0", "preview": "", "seq": 1}

id: 2
data: {"event": "message.delta", "run_id": "run_e405932666ab4dea82f9e666c333be78", "timestamp": 1793619579.0632918, "delta": "\n\nLinux", "seq": 2}

id: 3
data: {"event": "run.completed", "run_id": "run_e405932666ab4dea82f9e666c333be78", "timestamp": 1793619579.247749, "output": "Linux", "usage": {"input_tokens": 40301, "output_tokens": 44, "total_tokens": 40345}, "runtime": {"provider": "", "model": "", "route_source": "global"}, "completed": true, "partial": false, "interrupted": false, "seq": 3}

: stream closed

events: 0:tool.started, 1:tool.completed, 2:message.delta, 3:run.completed
PASS  ordered tool events and run.completed terminal frame
PASS  SSE ordering and terminal frame
```

### 3.6 Terminal status and cleanup

```
$ curl -sS -H 'Authorization: Bearer $HERMES_API_KEY' \
    http://127.0.0.1:8642/v1/runs/run_e405932666ab4dea82f9e666c333be78
HTTP 200
{"object": "hermes.run", "model": "hermes-agent", "runtime": {"provider": "", "model": "", "route_source": "global"}, "created_at": 1793619552.14416, "pending_controls": [], "run_id": "run_e405932666ab4dea82f9e666c333be78", "status": "completed", "session_id": "run_e405932666ab4dea82f9e666c333be78", "admission_id": "4b0b82cf906849049ea89de49f6f218b", "execution_generation": 1, "output": "Linux", "usage": {"input_tokens": 40301, "output_tokens": 44, "total_tokens": 40345}}
PASS  GET /v1/runs/{id} -> completed
run_id=run_e405932666ab4dea82f9e666c333be78 session_id=run_e405932666ab4dea82f9e666c333be78

$ curl -sS -w '\nHTTP %{http_code}\n' -X DELETE -H 'Authorization: Bearer $HERMES_API_KEY' \
    http://127.0.0.1:8642/api/sessions/run_e405932666ab4dea82f9e666c333be78
{"object": "hermes.session.deleted", "id": "run_e405932666ab4dea82f9e666c333be78", "deleted": true}
HTTP 200

RESULT: smoke test passed
```

## 4. Acceptance

| Criterion | Evidence |
|---|---|
| capabilities returns the expected feature flags | §3.3: every flag the relay gates on is `true`; `cors` and `audio_api` are `false` as designed |
| a run completes and emits tool events | §3.4–§3.6: `202` admission, `tool.started`/`tool.completed` for `terminal`, `run.completed` with `completed: true`, status `completed` |
| bound to loopback | §3.1: `127.0.0.1:8642`, no `0.0.0.0` |
| authenticated | §3.2: unauthenticated call refused `401` |
| local-only at runtime | local model provider; `features.audio_api: false`; no cloud STT/TTS/LLM in the path |

## 5. Notes that affect the relay adapter

- **`/v1` is the launch profile.** This install runs the multiplexed gateway
  (`gateway.multiplex_profiles: true`), so `http://127.0.0.1:8642/v1/*` serves the launch (default)
  profile and `/p/<profile>/v1/*` serves a named profile. `API_SERVER_KEY` is deliberately **not** in
  the process-global secret allowlist: a named profile's route authenticates with the key from
  `~/.hermes/profiles/<profile>/.env`. Observed today — the devops profile has no `API_SERVER_KEY`
  of its own, so:

  ```
  $ curl -sS -w '\nHTTP %{http_code}\n' http://127.0.0.1:8642/p/devops/v1/capabilities
  {"error": {"message": "Invalid gateway API key (API_SERVER_KEY)", "type": "gateway_auth_error", "code": "gateway_auth_failed"}}
  HTTP 401

  $ curl -sS -w '\nHTTP %{http_code}\n' http://127.0.0.1:8642/p/devops/health
  {"status": "ok", "platform": "hermes-agent", "version": "0.22.3"}
  HTTP 200
  ```

  The relay uses `/v1` and the default profile, so this does not affect it. Pointing the relay at a
  named profile later requires adding that profile's own `API_SERVER_KEY`.
- **Session identity.** With a session key that is new, `session_id == run_id` (§3.6). With a key that
  is already bound to a conversation, the status body reports the existing session instead — the
  adapter must read `session_id` back, never derive it from the run id.
- **Event buffer vs status.** The SSE backlog is swept 300 s after the last subscriber detaches
  (`404 run_not_found` on `/events`) while `GET /v1/runs/{id}` keeps answering `200`. Reconnect with
  `?last_seq=N` or `Last-Event-ID` inside that window, or fall back to polling status.
- **Approvals.** `approvals.mode: smart` auto-approves ordinary commands, so the smoke turn ran
  unattended. A prompt that does open stalls the run until it is answered or times out (120 s) — see
  [`research/hermes-api.md`](../research/hermes-api.md) §6–§7.
- **Toolsets.** The run's tool surface comes from `platform_toolsets.api_server`
  (`hermes-api-server` plus `clarify` on this host). Removing `clarify` from that list stops
  `clarify.request` events from being produced; it does not affect `tool.started`/`tool.completed`.

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `401 gateway_auth_failed` | `API_SERVER_KEY` mismatch (relay `.env` vs `~/.hermes/.env`), or a named-profile route without that profile's own key | align the relay's `HERMES_API_KEY`; add the profile key for `/p/<profile>/v1` |
| connection refused on `127.0.0.1:8642` | gateway down, or `API_SERVER_ENABLED` not set | `hermes gateway status`; start it; ask before restarting |
| `404 run_not_found` on `/events` | event buffer swept 300 s after the last subscriber | poll `GET /v1/runs/{id}`; reconnect within the window |
| run never finishes, no tool events | an approval prompt is open, or the toolset lacks the tool | inspect `pending_controls` on the status body; check `platform_toolsets.api_server` |
| `409 permission_denied` | the session belongs to another surface (e.g. web UI) | use an `api_server`-owned session or a fresh session key |
| `/v1/capabilities` returns a flag as `false` | feature disabled on this build | gate the relay on the flag; do not assume the endpoint exists |

## 7. Reproduction

```
HERMES_API_KEY=$(grep -E '^API_SERVER_KEY=' ~/.hermes/.env | cut -d= -f2-) \
  scripts/smoke_hermes_api.sh 2>&1 | tee /tmp/hermes-api-smoke.txt
```

Takes about 30 s (one real local model turn). Override the endpoint with `HERMES_API_BASE` and the
wait budget with `HERMES_SMOKE_WAIT`. `scripts/probe_hermes_api.sh` regenerates the wider contract
transcripts in [`research/hermes-api.md`](../research/hermes-api.md).

## References

- [`research/hermes-api.md`](../research/hermes-api.md) — the observed API contract (runs, SSE,
  approvals, clarify, sessions, ownership, attachments).
- [`RUNBOOK.md`](../RUNBOOK.md) — day-2 operations, including the gateway no-restart rule.
- [`ARCHITECTURE.md`](../ARCHITECTURE.md) — relay → Hermes topology.
