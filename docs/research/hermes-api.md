# Hermes API contract for the relay

> Status: **observed.** Every behaviour in this document was exercised against a running gateway and
> is quoted with the response it produced. Nothing here is inferred from source alone: where an
> implementation detail is mentioned it is backed by the transcript directly above it.
>
> Environment: Hermes Agent **v0.22.3** (`/health` reports `"version": "0.22.3"`), gateway with
> `platforms.api_server.enabled: true`, listening on `127.0.0.1:8642` (loopback only), and a **local**
> model provider — no cloud LLM, STT or TTS is involved in any transcript below.

Scope: exactly the surface the voice relay needs to drive a Hermes turn, stream its events, stop it,
answer approvals and clarify questions, and keep its session identity stable. The relay never exposes
this API beyond the host.

The bearer token appears as `$HERMES_API_KEY` in the commands (the probe script redacts the real value
as `***` in its output). Everything else is verbatim.

Reproduce with `scripts/probe_hermes_api.sh`; see [§14](#14-reproduction).

## Contents

1. [Transport, auth and error envelope](#1-transport-auth-and-error-envelope)
2. [Capabilities](#2-capabilities)
3. [Starting a run — `POST /v1/runs`](#3-starting-a-run--post-v1runs)
4. [Event stream — `GET /v1/runs/{id}/events`](#4-event-stream--get-v1runsidevents)
5. [Run status and pending controls — `GET /v1/runs/{id}`](#5-run-status-and-pending-controls--get-v1runsid)
6. [Approval responses](#6-approval-responses)
7. [Clarify responses](#7-clarify-responses)
8. [Stop](#8-stop)
9. [Sessions — `/api/sessions/*`](#9-sessions--apisessions)
10. [Surface ownership — the 409](#10-surface-ownership--the-409)
11. [Attachments — inline images only](#11-attachments--inline-images-only)
12. [X-Hermes-Session-Key](#12-x-hermes-session-key)
13. [Adapter checklist and failure-mode table](#13-adapter-checklist-and-failure-mode-table)
14. [Reproduction](#14-reproduction)

## 1. Transport, auth and error envelope

- Base URL in the examples: `http://127.0.0.1:8642`. The bind address is configuration, not part of the
  contract; the relay only ever talks to loopback.
- `GET /health` is unauthenticated and answers `{"status": "ok", "platform": "hermes-agent", "version": "0.22.3"}` with `200`.
- **Every other endpoint requires `Authorization: Bearer <API_SERVER_KEY>`.** The key must be at least
  16 characters or the API server refuses to start.
- Auth failure, with or without a header:

```
$ curl -sS -w '\nHTTP %{http_code}\n' http://127.0.0.1:8642/v1/capabilities
{"error": {"message": "Invalid gateway API key (API_SERVER_KEY)", "type": "gateway_auth_error", "code": "gateway_auth_failed"}}
HTTP 401
```

```
$ curl -sS -w '\nHTTP %{http_code}\n' -H 'Authorization: Bearer not-the-key' http://127.0.0.1:8642/v1/capabilities
{"error": {"message": "Invalid gateway API key (API_SERVER_KEY)", "type": "gateway_auth_error", "code": "gateway_auth_failed"}}
HTTP 401
```

Note the shape: this one carries **no `param` field**, unlike the request-validation errors in later
sections.

- Request-validation and state errors use the envelope
  `{"error": {"message": ..., "type": "invalid_request_error", "param": ..., "code": ...}}`. Some
  messages come with `"code": null` (see §3), some with a `param` pointer into the request body (see
  §11), some with neither `param` nor `code` (see §12).
- No CORS: `features.cors` is `false`. The relay is a same-host server-side client; there is no browser
  preflight path.
- No audio endpoints: `features.audio_api` and `features.realtime_voice` are both `false`. Speech is
  entirely the relay's own business — it hands Hermes text and receives text plus lifecycle events.

## 2. Capabilities

`GET /v1/capabilities` is the feature gate. The relay must read it at startup and refuse to start (or
degrade explicitly) when a required flag is missing, rather than assume an endpoint exists.

```
$ curl -sS -H 'Authorization: Bearer $HERMES_API_KEY' http://127.0.0.1:8642/v1/capabilities
```

```json
{
    "object": "hermes.api_server.capabilities",
    "platform": "hermes-agent",
    "model": "hermes-agent",
    "auth": {
        "type": "bearer",
        "required": true
    },
    "runtime": {
        "mode": "server_agent",
        "tool_execution": "server",
        "split_runtime": false,
        "description": "The API server creates a server-side Hermes AIAgent; tools execute on the API-server host unless a future explicit split-runtime mode is enabled."
    },
    "features": {
        "chat_completions": true,
        "chat_completions_streaming": true,
        "responses_api": true,
        "responses_streaming": true,
        "run_submission": true,
        "runs_idempotency": {
            "supported": true,
            "durable": true,
            "retention_seconds": 86400
        },
        "run_status": true,
        "run_events_sse": true,
        "run_stop": true,
        "run_steer": true,
        "run_approval_response": true,
        "tool_progress_events": true,
        "approval_events": true,
        "session_resources": true,
        "model_options": true,
        "session_chat": true,
        "session_chat_streaming": true,
        "session_fork": true,
        "session_model_lock": true,
        "reasoning_streaming": true,
        "admin_config_rw": false,
        "jobs_admin": false,
        "memory_write_api": false,
        "skills_api": true,
        "audio_api": false,
        "realtime_voice": false,
        "session_continuity_header": "X-Hermes-Session-Id",
        "session_key_header": "X-Hermes-Session-Key",
        "run_unknown_resolution": true,
        "cors": false,
        "browser_extension_control": {
            "enabled": false,
            "protocol_version": 1,
            "capabilities": [
                "browser_back",
                "browser_click",
                "browser_navigate",
                "browser_press",
                "browser_screenshot",
                "browser_scroll",
                "browser_snapshot",
                "browser_tab_activate",
                "browser_tabs",
                "browser_type",
                "controller.noop"
            ],
            "artifact_capabilities": [
                "browser_artifact_download",
                "browser_artifact_upload"
            ],
            "developer_capabilities": [
                "browser_cdp",
                "browser_evaluate"
            ],
            "developer_mode": false,
            "artifact_transport": {
                "upload": {
                    "method": "POST",
                    "path": "/v1/artifacts/upload"
                },
                "download": {
                    "method": "GET",
                    "path": "/v1/artifacts/download/{artifact_id}"
                },
                "max_bytes": 10485760,
                "ttl_seconds": 300.0,
                "allowed_mime_types": [
                    "application/json",
                    "application/pdf",
                    "image/gif",
                    "image/jpeg",
                    "image/png",
                    "image/webp",
                    "text/plain"
                ]
            },
            "real_browser_actions": true,
            "transports": {
                "local_vps": "websocket-subprotocol-ticket",
                "cloud": "authenticated-gateway-rpc"
            }
        }
    },
    "endpoints": {
        "health": {"method": "GET", "path": "/health"},
        "health_detailed": {"method": "GET", "path": "/health/detailed"},
        "models": {"method": "GET", "path": "/v1/models"},
        "model_options": {"method": "GET", "path": "/api/model/options"},
        "chat_completions": {"method": "POST", "path": "/v1/chat/completions"},
        "responses": {"method": "POST", "path": "/v1/responses"},
        "runs": {"method": "POST", "path": "/v1/runs"},
        "run_status": {"method": "GET", "path": "/v1/runs/{run_id}"},
        "run_events": {"method": "GET", "path": "/v1/runs/{run_id}/events"},
        "run_approval": {"method": "POST", "path": "/v1/runs/{run_id}/approval"},
        "run_steer": {"method": "POST", "path": "/v1/runs/{run_id}/steer"},
        "run_unknown_resolution": {"method": "POST", "path": "/v1/runs/{run_id}/resolve-unknown"},
        "run_stop": {"method": "POST", "path": "/v1/runs/{run_id}/stop"},
        "skills": {"method": "GET", "path": "/v1/skills"},
        "toolsets": {"method": "GET", "path": "/v1/toolsets"},
        "sessions": {"method": "GET", "path": "/api/sessions"},
        "session_create": {"method": "POST", "path": "/api/sessions"},
        "session": {"method": "GET", "path": "/api/sessions/{session_id}"},
        "session_update": {"method": "PATCH", "path": "/api/sessions/{session_id}"},
        "session_delete": {"method": "DELETE", "path": "/api/sessions/{session_id}"},
        "session_messages": {"method": "GET", "path": "/api/sessions/{session_id}/messages"},
        "session_fork": {"method": "POST", "path": "/api/sessions/{session_id}/fork"},
        "session_chat": {"method": "POST", "path": "/api/sessions/{session_id}/chat"},
        "session_chat_stream": {"method": "POST", "path": "/api/sessions/{session_id}/chat/stream"},
        "session_model_lock": {"method": "POST", "path": "/api/sessions/{session_id}/model"},
        "browser_control_register": {"method": "POST", "path": "/v1/browser-control/register"},
        "browser_control_ws": {"method": "GET", "path": "/v1/browser-control/ws"},
        "artifact_upload": {"method": "POST", "path": "/v1/artifacts/upload"},
        "artifact_download": {"method": "GET", "path": "/v1/artifacts/download/{artifact_id}"}
    }
}
```

(The raw body is a single line of JSON; the `endpoints` map is expanded here for readability. Field
names, values and ordering are unchanged.)

Flags the relay must gate on, and what breaks without them:

| Flag | Needed for | Failure if absent |
|---|---|---|
| `run_submission` | `POST /v1/runs` | no run-based turn at all |
| `run_events_sse` | `GET /v1/runs/{id}/events` | only polling `GET /v1/runs/{id}` |
| `run_stop` | `POST /v1/runs/{id}/stop` | must let turns finish |
| `run_approval_response` + `approval_events` | approval prompts | dangerous tools resolve without the relay's input |
| `session_resources` | `/api/sessions/*` | no session create/list/delete |
| `session_key_header` | `X-Hermes-Session-Key` | memory scope must come from `session_id` alone |

## 3. Starting a run — `POST /v1/runs`

Request fields the relay uses:

| Field / header | Meaning |
|---|---|
| `input` | user message: a string, or a content array (`{"type":"text","text":...}`, `{"type":"image_url",...}`) |
| `session_id` | bind the turn to an API-server session (see §10 for the ownership rule) |
| `X-Hermes-Session-Key` | declared conversation key; resolves to the session already bound to that key (§12) |
| `Idempotency-Key` | replay-safe retry; identical payload replays the original run, a different payload under the same key is `409 idempotency_key_conflict` |

Admission returns `202` with the run id, and echoes the declared session key:

```
$ curl -sS -i -X POST -H 'Authorization: Bearer $HERMES_API_KEY' -H 'Content-Type: application/json' \
  -H 'X-Hermes-Session-Key: voice-relay-probe' -H 'Idempotency-Key: probe-1793615933-4116590' \
  -d '{"input":"Use your terminal tool to run the shell command `uname -s`, then reply with only the output."}' \
  http://127.0.0.1:8642/v1/runs
```

```
HTTP/1.1 202 Accepted
X-Hermes-Session-Key: voice-relay-probe
{"run_id": "run_1a360b36daf6412f9757d2bdf5b918b1", "status": "started", "replayed": false}
```

**Session identity.** With neither `session_id` nor a session key, the run *is* its own session:
`run_id == session_id`. With a declared session key, the turn lands in whatever session that key was
previously bound to — observed: the run above reports `session_id: run_38984857236e4d79a256816e9238072d`
and `execution_generation: 9` from `GET /v1/runs/run_1a360b36daf6412f9757d2bdf5b918b1`, i.e. the ninth
turn of that conversation. The relay must therefore never assume `session_id == run_id` when it uses a
session key; it must read `session_id` back from the status body.

**Unknown session ids are adopted, not refused.** A `session_id` that does not exist yet is created on
the fly with `source: api_server`. Only an existing session owned by *another surface* is refused — see
§10.

```
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' -d '{"input":"hi","session_id":"api_nosuch"}' \
  http://127.0.0.1:8642/v1/runs
{"run_id": "run_95f4cac1cc414fb6ae66177878a7ee72", "status": "started", "replayed": false}
HTTP 202

$ curl -sS -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/v1/runs/run_95f4cac1cc414fb6ae66177878a7ee72
{"object": "hermes.run", "model": "hermes-agent", "runtime": {"provider": "", "model": "", "route_source": "global"}, "created_at": 1793616572.2644162, "pending_controls": [], "run_id": "run_95f4cac1cc414fb6ae66177878a7ee72", "status": "completed", "session_id": "api_nosuch", "admission_id": "783f1dd8b4654c549a99f835fe702224", "execution_generation": 1, "output": "Hey. What are we working on?", "usage": {"input_tokens": 20095, "output_tokens": 16, "total_tokens": 20111}}
```

The new session row carries `"source": "api_server"` in the store, so the relay may bind it freely on
later turns.

**Idempotent replay.** Repeating the identical request under the same key replays the original:

```
$ curl -sS -i -X POST -H 'Authorization: Bearer $HERMES_API_KEY' -H 'Content-Type: application/json' \
  -H 'X-Hermes-Session-Key: voice-relay-probe' -H 'Idempotency-Key: probe-1793615933-4116590' \
  -d '{"input":"Use your terminal tool to run the shell command `uname -s`, then reply with only the output."}' \
  http://127.0.0.1:8642/v1/runs
```

```
HTTP/1.1 202 Accepted
Idempotency-Replayed: true
X-Hermes-Session-Key: voice-relay-probe
{"run_id": "run_1a360b36daf6412f9757d2bdf5b918b1", "status": "completed", "replayed": true}
```

The replayed body carries the *current* status of the original run (`completed` here, not `started`) and
the `Idempotency-Replayed: true` header. Retention is 86400 s and the store is durable
(`features.runs_idempotency`), so the relay may retry across a process restart inside that window.

A different payload under the same key is refused:

```
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' -H 'X-Hermes-Session-Key: voice-relay-probe' \
  -H 'Idempotency-Key: probe-1793615933-4116590' -d '{"input":"something else entirely"}' \
  http://127.0.0.1:8642/v1/runs
{"error": {"message": "Idempotency-Key was already used with a different request payload", "type": "invalid_request_error", "param": null, "code": "idempotency_key_conflict"}}
HTTP 409
```

Malformed submissions (note `"code": null` in these three):

```
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8642/v1/runs
{"error": {"message": "Missing 'input' field", "type": "invalid_request_error", "param": null, "code": null}}
HTTP 400

$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' -d '{"input":[]}' http://127.0.0.1:8642/v1/runs
{"error": {"message": "Missing 'input' field", "type": "invalid_request_error", "param": null, "code": null}}
HTTP 400

$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' -d '{not json' http://127.0.0.1:8642/v1/runs
{"error": {"message": "Invalid JSON", "type": "invalid_request_error", "param": null, "code": null}}
HTTP 400
```

## 4. Event stream — `GET /v1/runs/{id}/events`

SSE. Frames are `id: <seq>` then `data: <json>`; the JSON carries `event`, `run_id`, `timestamp` and
`seq`. The stream opens with a `: open` comment frame (so the client's first byte arrives before the
first event), sends `: keepalive` comments while idle (observed roughly every 15–20 s), and ends a
finished run with `: stream closed`. `seq` starts at 0 and increments by one per event; `id:` repeats
the same value.

Full live transcript of a tool-using turn:

```
$ curl -sS -N -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/v1/runs/run_1a360b36daf6412f9757d2bdf5b918b1/events
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

Event names observed on this surface:

| Event | Payload beyond the envelope |
|---|---|
| `tool.started` | `tool`, `preview`, `tool_call_id` |
| `tool.completed` | `tool`, `duration`, `error` (bool), `tool_call_id`, `preview` |
| `approval.request` | `kind: "approval"`, `prompt_id`, `execution_generation`, `command`, `description`, `choices` |
| `approval.settled` | `prompt_id`, `execution_generation` |
| `clarify.request` | `kind: "clarify"`, `prompt_id`, `execution_generation`, `question`, `choices`, `multi_select` |
| `message.delta` | `delta` |
| `run.completed` | `output`, `usage`, `runtime`, `completed`, `partial`, `interrupted` |
| `run.cancelled` | `completed`, `partial`, `interrupted`, `turn_exit_reason` |
| `run.failed` | `error` (human-readable string), `completed`, `partial`, `interrupted` |

The implementation's wider vocabulary also names `message.interim`, `tool.start`, `tool.complete`,
`approval.responded` and `run.started`; on this build the run stream emitted `message.delta`,
`tool.started`, `tool.completed` and `approval.settled` instead. Build the parser around the names above
and treat anything unknown as an ignorable event — do not require `message.interim`.

`run.completed` and `run.cancelled` are the terminal frames; after either, the server appends
`: stream closed` and the connection ends. `turn_exit_reason` values observed: `interrupted_during_api_call`
(stop while the model was answering) and `interrupted_by_user` (stop during a tool call).

Ordering guarantee observed in the approval turn: the tool call is announced before the prompt
(`tool.started` → `approval.request` → `approval.settled` → `tool.completed` → `message.delta` →
`run.completed`). See §6.

**Reconnect / replay.** `GET /v1/runs/{id}/events` accepts `?last_seq=N` or a `Last-Event-ID: N` header
and replays everything after `N` before switching to live. Replaying a finished run whose buffer is still
retained returns the full transcript:

```
$ curl -sS -w '\nHTTP %{http_code}\n' --max-time 6 -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/v1/runs/run_1a360b36daf6412f9757d2bdf5b918b1/events
: open

id: 0
data: {"event": "tool.started", ...}
...
id: 3
data: {"event": "run.completed", ...}

: stream closed

HTTP 200
```

A single run's backlog holds at most 1000 events; if a client asks for a sequence number older than the
oldest retained one, the stream first emits a `replay.truncated` frame carrying `oldest_retained_seq`
(plus `requested_seq` when the client supplied one). That frame was **not exercised** in this probe — a
normal turn produces a handful of events, well under the backlog limit — so treat it as a defensive case:
if it arrives, fall back to `GET /v1/runs/{id}` for the final state.

**The event buffer is temporary; the status is not.** The transport buffer is swept 300 s after the last
subscriber detaches. Re-requesting events for a run that finished long ago gives a 404 while
`GET /v1/runs/{id}` still answers `200` with the terminal status:

```
$ curl -sS -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/v1/runs/run_8c0e44f02f334d4bab21b87e04c87b27
{"object": "hermes.run", "model": "hermes-agent", "runtime": {"provider": "", "model": "", "route_source": "global"}, "created_at": 1793613711.5664277, "pending_controls": [], "run_id": "run_8c0e44f02f334d4bab21b87e04c87b27", "status": "completed", "session_id": "run_38984857236e4d79a256816e9238072d", "admission_id": "db70b2b75d6040928a6fef5414b41be2", "execution_generation": 5, "output": "Linux", "usage": {"input_tokens": 40873, "output_tokens": 44, "total_tokens": 40917}}

$ curl -sS -w '\nHTTP %{http_code}\n' --max-time 6 -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/v1/runs/run_8c0e44f02f334d4bab21b87e04c87b27/events
{"error": {"message": "Run not found: run_8c0e44f02f334d4bab21b87e04c87b27", "type": "invalid_request_error", "param": null, "code": "run_not_found"}}
HTTP 404
```

Consequence for the adapter: a dropped stream must be resumed within the retention window (or by polling
status); once the buffer is gone, `run_not_found` on `/events` does **not** mean the run was lost.

A turn that dies before producing output ends with a single `run.failed` frame carrying the message the
agent would show a human (observed on a turn that aborted during agent construction):

```
: open

id: 0
data: {"event": "run.failed", "run_id": "run_2ef6221946d04ac8b81aa54992a1f439", "timestamp": 1793611503.3650925, "error": "\u26a0\ufe0f Something went wrong and I couldn't finish this reply.\nUse /retry to try again, or /new to start a fresh conversation. Technical details are in the gateway log (`hermes logs`).\n\nThis turn did not complete. Some actions may already have run; verify their effects before resending.", "completed": false, "partial": false, "interrupted": false, "seq": 0}

: stream closed
```

The relay should surface `error` as-is; there is no machine-readable failure code in the frame.

Unknown run ids on either endpoint:

```
$ curl -sS -w '\nHTTP %{http_code}\n' --max-time 6 -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/v1/runs/run_nope/events
{"error": {"message": "Run not found: run_nope", "type": "invalid_request_error", "param": null, "code": "run_not_found"}}
HTTP 404

$ curl -sS -w '\nHTTP %{http_code}\n' -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/v1/runs/run_nope
{"error": {"message": "Run not found: run_nope", "type": "invalid_request_error", "param": null, "code": "run_not_found"}}
HTTP 404
```

## 5. Run status and pending controls — `GET /v1/runs/{id}`

Body fields: `object`, `model`, `runtime`, `created_at`, `pending_controls`, `run_id`, `status`,
`session_id`, `admission_id`, `execution_generation`, `output`, `usage`.

Status vocabulary observed: `queued`, `running`, `waiting_for_approval`, `stopping`, `completed`,
`cancelled`, `failed`.

`pending_controls` is `[]` unless a control is open. Two shapes:

- approval — `{"kind": "approval", "prompt_id": ..., "execution_generation": <int>, "command": ...,
  "description": ..., "choices": ["once", "deny", "session", "always"]}`
- clarify — `{"kind": "clarify", "prompt_id": ..., "execution_generation": <int>, "question": ...,
  "choices": [...], "multi_select": <bool>}`

No `timeout_s` field was present in either observed entry; the timeout is server-side configuration
(`approvals.timeout`, 120 s in this deployment).

Status while an approval is pending (`waiting_for_approval`), polled at 2 s intervals:

```
$ curl -sS -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/v1/runs/run_d356f9d7be3f4a18a5fc65caa0768db3
status=running pending=[]
status=running pending=[]
status=running pending=[]
status=running pending=[]
status=running pending=[]
status=running pending=[]
status=running pending=[]
status=running pending=[]
status=running pending=[]
status=waiting_for_approval pending=[{"kind": "approval", "prompt_id": "a8ff8ef17ed04d718fcd6bc9e4cd3210", "execution_generation": 1, "command": "rm -rf /tmp/hermes-api-probe-dir", "description": "delete in root path", "choices": ["once", "deny", "session", "always"]}]
```

```json
{
    "object": "hermes.run",
    "model": "hermes-agent",
    "created_at": 1793615962.4834125,
    "pending_controls": [
        {
            "kind": "approval",
            "prompt_id": "a8ff8ef17ed04d718fcd6bc9e4cd3210",
            "execution_generation": 1,
            "command": "rm -rf /tmp/hermes-api-probe-dir",
            "description": "delete in root path",
            "choices": [
                "once",
                "deny",
                "session",
                "always"
            ]
        }
    ],
    "run_id": "run_d356f9d7be3f4a18a5fc65caa0768db3",
    "status": "waiting_for_approval",
    "session_id": "run_d356f9d7be3f4a18a5fc65caa0768db3",
    "admission_id": "d022fe58d1f0471fae56037259ab647b",
    "execution_generation": 1,
    "output": "",
    "usage": {}
}
```

**A pending clarify does not change the status.** While a clarify prompt was open, `GET /v1/runs/{id}`
reported `"status": "running"` with a populated `pending_controls`. An adapter that only looks at
`status` will miss clarify prompts entirely — it must watch `pending_controls` (or the SSE
`clarify.request` event) instead.

```json
{
    "object": "hermes.run",
    "model": "hermes-agent",
    "created_at": 1793615988.8645089,
    "pending_controls": [
        {
            "kind": "clarify",
            "prompt_id": "31fd319393",
            "execution_generation": 1,
            "question": "Which colour do you prefer, red or blue?\nReply \"skip\" to skip this question.",
            "choices": [
                "Red (Recommended)",
                "Blue"
            ],
            "multi_select": false
        }
    ],
    "run_id": "run_18953b8edeb3414db8c85b9df68b8861",
    "status": "running",
    "session_id": "run_18953b8edeb3414db8c85b9df68b8861",
    "admission_id": "9aa092332dd64ff38effdda97cfeb979",
    "execution_generation": 1,
    "output": "",
    "usage": {}
}
```

`execution_generation` is the per-run control epoch the response endpoints validate against; it also
advances per turn in a long-lived session (observed climbing from 1 to 10 across keyed turns of one
conversation). Treat
it as opaque: echo back exactly the value from the pending control.

## 6. Approval responses

`POST /v1/runs/{run_id}/approval` accepts **exactly** `{"request_id", "execution_generation", "choice"}`
with `choice` one of `once`, `session`, `always`, `deny`. Any additional key is a 400; a generation that
is not the run's current one is a 409; a prompt that is not open (wrong id, or already answered) is a 409.

```
### approval with an extra key (invalid_params)
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"a8ff8ef17ed04d718fcd6bc9e4cd3210","execution_generation":1,"choice":"once","extra":true}' \
  http://127.0.0.1:8642/v1/runs/run_d356f9d7be3f4a18a5fc65caa0768db3/approval
{"error": {"message": "invalid_params", "type": "invalid_request_error", "param": null, "code": "invalid_params"}}
HTTP 400

### approval with a stale execution_generation
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"a8ff8ef17ed04d718fcd6bc9e4cd3210","execution_generation":7,"choice":"once"}' \
  http://127.0.0.1:8642/v1/runs/run_d356f9d7be3f4a18a5fc65caa0768db3/approval
{"error": {"message": "stale_generation", "type": "invalid_request_error", "param": null, "code": "stale_generation"}}
HTTP 409

### approval with an unknown request_id
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"not-a-prompt","execution_generation":1,"choice":"once"}' \
  http://127.0.0.1:8642/v1/runs/run_d356f9d7be3f4a18a5fc65caa0768db3/approval
{"error": {"message": "approval_not_pending", "type": "invalid_request_error", "param": null, "code": "approval_not_pending"}}
HTTP 409

### approval accepted
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"a8ff8ef17ed04d718fcd6bc9e4cd3210","execution_generation":1,"choice":"once"}' \
  http://127.0.0.1:8642/v1/runs/run_d356f9d7be3f4a18a5fc65caa0768db3/approval
{"run_id": "run_d356f9d7be3f4a18a5fc65caa0768db3", "status": "resolved", "prompt_id": "a8ff8ef17ed04d718fcd6bc9e4cd3210"}
HTTP 200

### same approval answered twice
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"a8ff8ef17ed04d718fcd6bc9e4cd3210","execution_generation":1,"choice":"once"}' \
  http://127.0.0.1:8642/v1/runs/run_d356f9d7be3f4a18a5fc65caa0768db3/approval
{"error": {"message": "approval_not_pending", "type": "invalid_request_error", "param": null, "code": "approval_not_pending"}}
HTTP 409
```

Success answers `200` with `{"run_id", "status": "resolved", "prompt_id"}` — not the run status. The
prompt is consumed by the first accepted response; a retry is a 409, so the adapter must treat
`approval_not_pending` on retry as "already answered" rather than as an error to surface.

Event ordering around a resolved approval:

```
$ curl -sS -N -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/v1/runs/run_d356f9d7be3f4a18a5fc65caa0768db3/events
: open

id: 0
data: {"event": "tool.started", "run_id": "run_d356f9d7be3f4a18a5fc65caa0768db3", "timestamp": 1793615980.3785095, "tool": "terminal", "preview": "rm -rf /tmp/hermes-api-probe-dir", "tool_call_id": "call_186747da8cd53c5efa2b6fa6", "seq": 0}

id: 1
data: {"event": "approval.request", "run_id": "run_d356f9d7be3f4a18a5fc65caa0768db3", "timestamp": 1793615980.4475675, "kind": "approval", "prompt_id": "a8ff8ef17ed04d718fcd6bc9e4cd3210", "execution_generation": 1, "command": "rm -rf /tmp/hermes-api-probe-dir", "description": "delete in root path", "choices": ["once", "deny", "session", "always"], "seq": 1}

id: 2
data: {"event": "approval.settled", "run_id": "run_d356f9d7be3f4a18a5fc65caa0768db3", "timestamp": 1793615980.8709047, "prompt_id": "a8ff8ef17ed04d718fcd6bc9e4cd3210", "execution_generation": 1, "seq": 2}

id: 3
data: {"event": "tool.completed", "run_id": "run_d356f9d7be3f4a18a5fc65caa0768db3", "timestamp": 1793615980.921361, "tool": "terminal", "duration": 0, "error": false, "tool_call_id": "call_186747da8cd53c5efa2b6fa6", "preview": "", "seq": 3}

id: 4
data: {"event": "message.delta", "run_id": "run_d356f9d7be3f4a18a5fc65caa0768db3", "timestamp": 1793615988.640202, "delta": "\n\ndone", "seq": 4}

id: 5
data: {"event": "run.completed", "run_id": "run_d356f9d7be3f4a18a5fc65caa0768db3", "timestamp": 1793615988.8504517, "output": "done", "usage": {"input_tokens": 40382, "output_tokens": 87, "total_tokens": 40469}, "runtime": {"provider": "", "model": "", "route_source": "global"}, "completed": true, "partial": false, "interrupted": false, "seq": 5}

: stream closed
```

The settled event is named `approval.settled` on this surface (the implementation also knows
`approval.responded`, but the stream emits `approval.settled`). Adapters should match on
`approval.settled` and treat any unknown event name as ignorable.

**When does a prompt appear at all?** Only when the deployment has an approval transport for the
session. In this gateway:

- `approvals.mode: manual` — every dangerous-class tool call produces a pending prompt.
- `approvals.mode: smart` — a guardian model decides first. An APPROVE resolves without any prompt
  (observed: `rm -rf /tmp/hermes-api-probe-dir` was auto-approved and the stream went
  `tool.started` → `tool.completed` with no `approval.request`). Only ESCALATE/DENY reach the relay.
- `approvals.mode: off` — nothing prompts.

The probe therefore flips the mode to `manual` for this section (and restores it), so the transcript
above is deterministic. With `smart`, the relay must tolerate turns that never produce a prompt.

On timeout (120 s here) the run continues down the deny branch; the relay does not have to answer.
Observed: the prompt stays in `pending_controls` until the deadline, which is measured from the moment
the prompt opens (it appeared 27 s after admission and expired 120 s later), then the tool fails and the
model explains:

```
id: 0
data: {"event": "tool.started", ..., "tool": "terminal", "preview": "rm -rf /tmp/hermes-api-timeout-dir", "seq": 0}

id: 1
data: {"event": "approval.request", ..., "prompt_id": "9f15391ab0a84744b2025b21fff0fb4f", "seq": 1}

id: 2
data: {"event": "tool.completed", ..., "tool": "terminal", "duration": 0, "error": true, "seq": 2}     <- prompt + 120 s

id: 3
data: {"event": "approval.settled", ..., "prompt_id": "9f15391ab0a84744b2025b21fff0fb4f", "seq": 3}

id: 4 .. id: 23
data: {"event": "message.delta", ...}

id: 24
data: {"event": "run.completed", ..., "output": "Blocked, not run \u2014 the rm -rf approval prompt timed out after 2 minutes without a response, so /tmp/hermes-api-timeout-dir was left untouched. I did not retry. Say the word and I'll run it again.", "seq": 24}

: stream closed
```

(Ids and payloads abbreviated where marked; the terminal frame is verbatim.) Note the ordering
difference against an answered prompt: on timeout `tool.completed` (`error: true`) arrives **before**
`approval.settled`, whereas an accepted approval settles first. An adapter must not assume a fixed order
between those two events — wait for `tool.completed`, and treat `approval.settled` as informational.

The run still ends `completed` (`output` carries the agent's explanation), not `failed`:

```
$ curl -sS -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/v1/runs/run_5645eed655ea48c888aefa7f3bdccc9b
{"object": "hermes.run", "model": "hermes-agent", "runtime": {"provider": "", "model": "", "route_source": "global"}, "created_at": 1793617906.9129689, "pending_controls": [], "run_id": "run_5645eed655ea48c888aefa7f3bdccc9b", "status": "completed", "session_id": "run_5645eed655ea48c888aefa7f3bdccc9b", "admission_id": "2a7bd160111d490ebe687d3cb37b82de", "execution_generation": 1, "output": "Blocked, not run \u2014 the rm -rf approval prompt timed out after 2 minutes without a response, so /tmp/hermes-api-timeout-dir was left untouched. I did not retry. Say the word and I'll run it again.", "usage": {"input_tokens": 40465, "output_tokens": 349, "total_tokens": 40814}}
```

## 7. Clarify responses

`POST /v1/runs/{run_id}/clarify` takes exactly `{"request_id", "execution_generation", "answer"}`.
`answer` is free text: it is accepted even when the prompt offers `choices` (the transcript answers
`"Red"`, which is a choice label minus the "(Recommended)" suffix).

```
### clarify with the wrong key name (invalid_params)
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"31fd319393","execution_generation":1,"choice":"Red"}' \
  http://127.0.0.1:8642/v1/runs/run_18953b8edeb3414db8c85b9df68b8861/clarify
{"error": {"message": "invalid_params", "type": "invalid_request_error", "param": null, "code": "invalid_params"}}
HTTP 400

### clarify with a stale execution_generation
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"31fd319393","execution_generation":0,"answer":"Red"}' \
  http://127.0.0.1:8642/v1/runs/run_18953b8edeb3414db8c85b9df68b8861/clarify
{"error": {"message": "stale_generation", "type": "invalid_request_error", "param": null, "code": "stale_generation"}}
HTTP 409

### clarify answered
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"31fd319393","execution_generation":1,"answer":"Red"}' \
  http://127.0.0.1:8642/v1/runs/run_18953b8edeb3414db8c85b9df68b8861/clarify
{"run_id": "run_18953b8edeb3414db8c85b9df68b8861", "status": "resolved", "prompt_id": "31fd319393"}
HTTP 200

### same clarify answered twice
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"31fd319393","execution_generation":1,"answer":"Red"}' \
  http://127.0.0.1:8642/v1/runs/run_18953b8edeb3414db8c85b9df68b8861/clarify
{"error": {"message": "approval_not_pending", "type": "invalid_request_error", "param": null, "code": "approval_not_pending"}}
HTTP 409
```

Note that a resolved clarify reports the **same error code as an approval** (`approval_not_pending`)
when repeated — the two prompt kinds share one pending-control table.

The prompt also arrives on the run's event stream, ahead of the tool result (separate capture; the
clarify prompt in it was answered with `{"request_id":"21e56fd507","execution_generation":1,"answer":"Red"}`):

```
$ curl -sS -N -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/v1/runs/run_546be8ab17284a698c266ba41bbae43c/events
: open

id: 0
data: {"event": "tool.started", "run_id": "run_546be8ab17284a698c266ba41bbae43c", "timestamp": 1793609847.9208407, "tool": "clarify", "preview": "Which colour do you prefer, red or blue?", "tool_call_id": "call_a26497498038feae7105693b", "seq": 0}

id: 1
data: {"event": "clarify.request", "run_id": "run_546be8ab17284a698c266ba41bbae43c", "timestamp": 1793609847.9595108, "kind": "clarify", "prompt_id": "21e56fd507", "execution_generation": 1, "question": "Which colour do you prefer, red or blue?\nReply \"skip\" to skip this question.", "choices": ["Red (Recommended)", "Blue"], "multi_select": false, "seq": 1}
```

**Prerequisite: the clarify toolset must be enabled for the API server.** By default `clarify` is
`enabled: false` for `platform_toolsets.api_server`, so the agent has no `clarify` tool and never emits
`clarify.request` — observed: a run told to use its clarify tool answered
`"No clarify tool is exposed in this session, so asking directly: which colour do you prefer, red or blue?"`
and completed. The endpoint still exists; it simply never fires. Enable it with:

```yaml
platform_toolsets:
  api_server:
    - hermes-api-server
    - clarify
```

The setting is picked up by the running gateway without a restart. `GET /v1/toolsets` reports the
effective state per platform, so the relay can check it at startup instead of guessing:

```
$ curl -sS -H 'Authorization: Bearer $HERMES_API_KEY' http://127.0.0.1:8642/v1/toolsets
... {"name": "clarify", "enabled": true, "configured": true, "tools": ["clarify"]} ...
```

(With the default configuration the same entry reads `"enabled": false`, and a run told to use its
clarify tool answers "No clarify tool is exposed in this session".)

## 8. Stop

`POST /v1/runs/{run_id}/stop` with an empty body interrupts the turn. It answers immediately:

```
$ curl -sS -w '\nHTTP %{http_code} in %{time_total}s\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' -d '{}' \
  http://127.0.0.1:8642/v1/runs/run_abd3b2c9ddfb447b80387aa07024aa91/stop
{"run_id": "run_abd3b2c9ddfb447b80387aa07024aa91", "status": "stopping", "admission_id": "b4e0f7b78c8c45d2b342d6f1c813f068"}
HTTP 200 in 0.002531s
```

The run then settles as `cancelled`:

```
$ curl -sS -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/v1/runs/run_abd3b2c9ddfb447b80387aa07024aa91
{"object": "hermes.run", "model": "hermes-agent", "created_at": 1793616019.7450743, "pending_controls": [], "run_id": "run_abd3b2c9ddfb447b80387aa07024aa91", "status": "cancelled", "session_id": "run_abd3b2c9ddfb447b80387aa07024aa91", "admission_id": "b4e0f7b78c8c45d2b342d6f1c813f068", "execution_generation": 1, "output": "Operation interrupted: waiting for model response (12.9s elapsed).", "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
```

Stopping an already-terminal run is not an error; it returns the terminal status body with `200`:

```
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' -d '{}' \
  http://127.0.0.1:8642/v1/runs/run_abd3b2c9ddfb447b80387aa07024aa91/stop
{"pending_controls": [], "run_id": "run_abd3b2c9ddfb447b80387aa07024aa91", "status": "cancelled", "session_id": "run_abd3b2c9ddfb447b80387aa07024aa91", "admission_id": "b4e0f7b78c8c45d2b342d6f1c813f068", "execution_generation": 1, "output": "Operation interrupted: waiting for model response (12.9s elapsed).", "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
HTTP 200
```

Note the field order differs between the two bodies (`pending_controls` first here); parse JSON, do not
pattern-match on order.

Unknown run:

```
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' -d '{}' \
  http://127.0.0.1:8642/v1/runs/run_doesnotexist/stop
{"error": {"message": "Run not found: run_doesnotexist", "type": "invalid_request_error", "param": null, "code": "run_not_found"}}
HTTP 404
```

Terminal frame after a stop:

```
$ curl -sS -N -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/v1/runs/run_abd3b2c9ddfb447b80387aa07024aa91/events
: open

id: 0
data: {"event": "run.cancelled", "run_id": "run_abd3b2c9ddfb447b80387aa07024aa91", "timestamp": 1793616035.1420393, "completed": false, "partial": false, "interrupted": true, "turn_exit_reason": "interrupted_during_api_call", "seq": 0}

: stream closed
```

The relay must treat `run.cancelled` with `interrupted: true` as its own requested outcome (the user
pressed stop), not as a failure.

## 9. Sessions — `/api/sessions/*`

All session endpoints are on the same bearer auth. A session created through the REST surface carries
`"source": "api_server"`.

Create (`201`):

```
$ curl -sS -X POST -H 'Authorization: Bearer $HERMES_API_KEY' -H 'Content-Type: application/json' \
  -d '{"title":"hermes-api probe 20261102-114037"}' http://127.0.0.1:8642/api/sessions
```

```json
{
    "object": "hermes.session",
    "session": {
        "id": "api_1793616037_8fb7f29f",
        "source": "api_server",
        "user_id": null,
        "model": null,
        "title": "hermes-api probe 20261102-114037",
        "started_at": 1793616037.8589854,
        "ended_at": null,
        "end_reason": null,
        "message_count": 0,
        "tool_call_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "estimated_cost_usd": null,
        "actual_cost_usd": null,
        "api_call_count": 0,
        "parent_session_id": null,
        "pinned": false,
        "archived": false,
        "hidden": false,
        "has_system_prompt": false,
        "has_model_config": false,
        "is_internal_child": false
    }
}
```

Titles are unique across the store; reusing one is refused:

```
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' -d '{"title":"hermes-api probe 20261102-114037"}' \
  http://127.0.0.1:8642/api/sessions
{"error": {"message": "Title 'hermes-api probe 20261102-114037' is already in use by session api_1793616037_8fb7f29f", "type": "invalid_request_error", "param": null, "code": "invalid_title"}}
HTTP 400
```

List (`200`). The listing is **not** filtered to the API surface — it returns sessions owned by other
surfaces too, each carrying its `source`. That is the cheap way for the relay to check ownership before
binding:

```
$ curl -sS -H 'Authorization: Bearer $HERMES_API_KEY' 'http://127.0.0.1:8642/api/sessions?limit=2'
```

```json
{
    "object": "list",
    "data": [
        {"id": "api_1793616037_8fb7f29f", "source": "api_server", "user_id": null, "model": null, "title": "hermes-api probe 20261102-114037", "started_at": 1793616037.8589854, "ended_at": null, "end_reason": null, "message_count": 0, "tool_call_count": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0, "estimated_cost_usd": null, "actual_cost_usd": null, "api_call_count": 0, "parent_session_id": null, "last_active": 1793616037.8589854, "preview": "", "pinned": false, "archived": false, "hidden": false, "has_system_prompt": false, "has_model_config": false, "is_internal_child": false},
        {"id": "run_abd3b2c9ddfb447b80387aa07024aa91", "source": "api_server", "user_id": "api", "model": "agent", "title": null, "started_at": 1793616019.747818, "ended_at": null, "end_reason": null, "message_count": 3, "tool_call_count": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0, "estimated_cost_usd": null, "actual_cost_usd": null, "api_call_count": 0, "parent_session_id": null, "last_active": 1793616035.0787342, "preview": "Use your terminal tool to run the shell command `sleep 45`, ...", "pinned": false, "archived": false, "hidden": false, "has_system_prompt": true, "has_model_config": true, "is_internal_child": false}
    ],
    "limit": 2,
    "offset": 0,
    "has_more": true
}
```

(Pretty-printed from the single-line body; the two records are verbatim.)

Get (`200`) returns the same `hermes.session` envelope for a session the relay owns; the same call on a
foreign session also answers `200` — reads are allowed across surfaces, only *turns* are not (§10).

Chat — one synchronous turn (`200`, object `hermes.session.chat.completion`):

```
$ curl -sS -w '\nHTTP %{http_code} in %{time_total}s\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' -d '{"message":"Reply with only the word pong."}' \
  http://127.0.0.1:8642/api/sessions/api_1793616037_8fb7f29f/chat
{"object": "hermes.session.chat.completion", "status": "completed", "session_id": "api_1793616037_8fb7f29f", "message": {"role": "assistant", "content": "pong"}, "usage": {"input_tokens": 20102, "output_tokens": 4, "total_tokens": 20106}, "runtime": {"provider": "", "model": "", "route_source": "global", "requested": {"provider": "", "model": ""}}}
HTTP 200 in 20.995578s
```

Chat stream (`text/event-stream`, `X-Hermes-Session-Id` response header). This is a **different event
namespace** from `/v1/runs/{id}/events`: the transcript shows `run.started`, `message.started`,
`assistant.delta`, `assistant.completed`, `run.completed`, `done`, with `seq` and `ts` fields instead of
`timestamp`. Do not reuse the `/v1/runs` event parser for it.

```
$ curl -sS -N --max-time 120 -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' -d '{"message":"Reply with only the word ping."}' \
  http://127.0.0.1:8642/api/sessions/api_1793616037_8fb7f29f/chat/stream
event: run.started
data: {"user_message": {"role": "user", "content": "Reply with only the word ping."}, "runtime": {"provider": "", "model": "", "route_source": "global", "requested": {"provider": "", "model": ""}}, "session_id": "api_1793616037_8fb7f29f", "run_id": "run_cf94ba1720674d66994cda7ecb47aa82", "seq": 1, "ts": 1793616058.9695501}

event: message.started
data: {"message": {"id": "msg_0cad65a004f24c839df913c39bbf540f", "role": "assistant"}, "session_id": "api_1793616037_8fb7f29f", "run_id": "run_cf94ba1720674d66994cda7ecb47aa82", "seq": 2, "ts": 1793616058.969557}

: keepalive

: keepalive

event: assistant.delta
data: {"message_id": "msg_0cad65a004f24c839df913c39bbf540f", "delta": "ping", "session_id": "api_1793616037_8fb7f29f", "run_id": "run_cf94ba1720674d66994cda7ecb47aa82", "seq": 3, "ts": 1793616088.1254058}

event: assistant.completed
data: {"session_id": "api_1793616037_8fb7f29f", "message_id": "msg_0cad65a004f24c839df913c39bbf540f", "content": "ping", "completed": true, "partial": false, "interrupted": false, "runtime": {"provider": "", "model": "", "route_source": "global", "requested": {"provider": "", "model": ""}}, "run_id": "run_cf94ba1720674d66994cda7ecb47aa82", "seq": 4, "ts": 1793616088.328084}

event: run.completed
data: {"session_id": "api_1793616037_8fb7f29f", "message_id": "msg_0cad65a004f24c839df913c39bbf540f", "completed": true, "partial": false, "interrupted": false, "messages": [{"role": "assistant", "content": "ping", "timestamp": 1793616088.1413448, "finish_reason": "stop", "reasoning": null}], "usage": {"input_tokens": 20116, "output_tokens": 3, "total_tokens": 20119}, "runtime": {"provider": "", "model": "", "route_source": "global", "requested": {"provider": "", "model": ""}}, "run_id": "run_cf94ba1720674d66994cda7ecb47aa82", "seq": 5, "ts": 1793616088.3281002}

event: done
data: {"session_id": "api_1793616037_8fb7f29f", "run_id": "run_cf94ba1720674d66994cda7ecb47aa82", "seq": 6, "ts": 1793616088.32815}
```

The `run_id` inside those frames identifies the session-stream turn, not a `/v1/runs` admission, and it
does not resolve on the run endpoints — after the stream finished,
`GET /v1/runs/run_cf94ba1720674d66994cda7ecb47aa82` answered:

```
{"error": {"message": "Run not found: run_cf94ba1720674d66994cda7ecb47aa82", "type": "invalid_request_error", "param": null, "code": "run_not_found"}}
HTTP 404
```

Keep the two id spaces apart: session-stream ids are for logging and for matching frames within that
stream; only `/v1/runs` ids can be polled, stopped or answered.

Messages (`200`). The transcript contains synthetic rows the adapter must filter: the user row carries
`display_kind: "internal_notification"`, and every turn appends a `session_meta` row with `content: null`.

```
$ curl -sS -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/api/sessions/api_1793616037_8fb7f29f/messages
{"object": "list", "session_id": "api_1793616037_8fb7f29f", "data": [{"id": 267955, "session_id": "api_1793616037_8fb7f29f", "role": "user", "content": "Reply with only the word pong.", "tool_call_id": null, "tool_calls": null, "tool_name": null, "timestamp": 1793616038.020481, "token_count": null, "finish_reason": null, "reasoning": null, "reasoning_content": null, "display_kind": "internal_notification"}, {"id": 267958, "session_id": "api_1793616037_8fb7f29f", "role": "assistant", "content": "pong", "tool_call_id": null, "tool_calls": null, "tool_name": null, "timestamp": 1793616058.7868426, "token_count": null, "finish_reason": "stop", "reasoning": null, "reasoning_content": null, "display_kind": null}, {"id": 267959, "session_id": "api_1793616037_8fb7f29f", "role": "session_meta", "content": null, "tool_call_id": null, "tool_calls": null, "tool_name": null, "timestamp": 1793616058.8709104, "token_count": null, "finish_reason": null, "reasoning": null, "reasoning_content": null, "display_kind": null}], "pagination": {"limit": 500, "offset": 0, "order": "latest", "returned": 3}}
```

Patch (`200`) updates the title and returns the full session object. Fork (`201`) copies the transcript
into a new session and **ends the parent** — observed: after the fork, `GET /api/sessions/api_1793616037_8fb7f29f`
reports `"end_reason": "branched"` with a non-null `ended_at`:

```
$ curl -sS -X POST -H 'Authorization: Bearer $HERMES_API_KEY' -H 'Content-Type: application/json' \
  -d '{}' http://127.0.0.1:8642/api/sessions/api_1793616037_8fb7f29f/fork
{"object": "hermes.session", "session": {"id": "api_1793616088_da9db4df", "source": "api_server", "user_id": null, "model": "agent", "title": "hermes-api probe 20261102-114037 renamed #2", "started_at": 1793616088.3776212, "ended_at": null, "end_reason": null, "message_count": 5, "tool_call_count": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0, "estimated_cost_usd": null, "actual_cost_usd": null, "api_call_count": 0, "parent_session_id": "api_1793616037_8fb7f29f", "pinned": false, "archived": false, "hidden": false, "has_system_prompt": true, "has_model_config": true, "is_internal_child": false}}
HTTP 201
```

The parent is closed by the fork — `GET` on it afterwards reports `end_reason: "branched"` and a set
`ended_at`. It still accepts further turns, though: a run bound to the ended parent completed with the
same `session_id`, so "branched" marks the split, it does not make the session unusable:

```
$ curl -sS -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/api/sessions/api_1793616037_8fb7f29f
{"object": "hermes.session", "session": {"id": "api_1793616037_8fb7f29f", "source": "api_server", "user_id": "api", "model": "agent", "title": "hermes-api probe 20261102-114037 renamed", "started_at": 1793616037.8589854, "ended_at": 1793616088.3875065, "end_reason": "branched", "message_count": 5, "tool_call_count": 0, "input_tokens": 3354, "output_tokens": 7, "cache_read_tokens": 36864, "cache_write_tokens": 0, "reasoning_tokens": 2, "estimated_cost_usd": 0.0, "actual_cost_usd": null, "api_call_count": 2, "parent_session_id": null, "pinned": false, "archived": false, "hidden": false, "has_system_prompt": true, "has_model_config": true, "is_internal_child": false}}
```

```
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' -d '{"input":"Reply with only the word after.","session_id":"api_1793616037_8fb7f29f"}' \
  http://127.0.0.1:8642/v1/runs
{"run_id": "run_8f9c0e5a21324a68bb45d420387bc5b3", "status": "started", "replayed": false}
HTTP 202

$ curl -sS -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/v1/runs/run_8f9c0e5a21324a68bb45d420387bc5b3
{"object": "hermes.run", ..., "status": "completed", "session_id": "api_1793616037_8fb7f29f", "run_id": "run_8f9c0e5a21324a68bb45d420387bc5b3", "output": "after", ...}
```

Model lock (`200`) pins the session's runtime; it needs a real model/provider pair, not the virtual
`hermes-agent` name:

```
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' -d '{"model":"hermes-agent"}' \
  http://127.0.0.1:8642/api/sessions/api_1793616037_8fb7f29f/model
{"error": {"message": "require_model_lock was set but no model/provider was provided", "type": "invalid_request_error", "param": null, "code": "missing_model"}}
HTTP 400

$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' -d '{"model":"agent","provider":"gx10-cluster"}' \
  http://127.0.0.1:8642/api/sessions/api_1793616037_8fb7f29f/model
{"object": "hermes.session.model_lock", "session_id": "api_1793616037_8fb7f29f", "runtime": {"provider": "gx10-cluster", "model": "agent", "route_source": "raw_request", "requested": {"provider": "gx10-cluster", "model": "agent"}, "model_lock": "accepted"}}
HTTP 200
```

Delete (`200`) and unknown ids:

```
$ curl -sS -w '\nHTTP %{http_code}\n' -X DELETE -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/api/sessions/api_1793616088_da9db4df
{"object": "hermes.session.deleted", "id": "api_1793616088_da9db4df", "deleted": true}
HTTP 200

$ curl -sS -w '\nHTTP %{http_code}\n' -X DELETE -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/api/sessions/api_doesnotexist
{"error": {"message": "Session not found: api_doesnotexist", "type": "invalid_request_error", "param": null, "code": "session_not_found"}}
HTTP 404
```

## 10. Surface ownership — the 409

A REST request may only bind a session whose stored `source` is `api_server`. Binding a session owned by
another surface is refused with `409 permission_denied`.

Verified with a session id taken from the store (`source: webui`):

```
# foreign session id discovered via sqlite (~/.hermes/state.db): f9766a0f167f

$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' -d '{"input":"hello","session_id":"f9766a0f167f"}' \
  http://127.0.0.1:8642/v1/runs
{"error": {"message": "permission_denied", "type": "invalid_request_error", "param": null, "code": "permission_denied"}}
HTTP 409
```

`POST /api/sessions/{foreign}/chat` hits the same refusal, but it surfaces as an **unhandled 500 with a
plain-text body**, not JSON:

```
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' -d '{"message":"hello"}' \
  http://127.0.0.1:8642/api/sessions/f9766a0f167f/chat
500 Internal Server Error

Server got itself in trouble
HTTP 500
```

The gateway log shows the cause (`hermes_state_runtime.RuntimeStoreError: permission_denied` raised while
persisting the session's runtime lock, with no handler converting it). Adapter consequence: **do not**
use the sessions chat door to probe ownership — check `source` from `GET /api/sessions` (or the same
record's `source` field) and bind with `/v1/runs`, which gives the documented 409.

Reads are not restricted: `GET /api/sessions/{foreign}` and `GET /api/sessions/{foreign}/messages` answer
`200`. Only turns are refused.

```
$ curl -sS -w '\nHTTP %{http_code}\n' -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/api/sessions/f9766a0f167f
{"object": "hermes.session", "session": {"id": "f9766a0f167f", "source": "webui", "user_id": null, "model": "agent", "title": "best portable LED light with AA or AAA batteries?", "started_at": 1793612762.7665777, "ended_at": null, "end_reason": null, "message_count": 35, "tool_call_count": 20, "input_tokens": 873702, "output_tokens": 7419, "cache_read_tokens": 774144, "cache_write_tokens": 0, "reasoning_tokens": 0, "estimated_cost_usd": 0.0, "actual_cost_usd": null, "api_call_count": 14, "parent_session_id": null, "pinned": false, "archived": false, "hidden": false, "has_system_prompt": true, "has_model_config": true, "is_internal_child": false}}
HTTP 200

$ curl -sS -w '\nHTTP %{http_code}\n' -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/api/sessions/f9766a0f167f/messages
{"object": "list", "session_id": "f9766a0f167f", "data": [{"id": 267796, "session_id": "f9766a0f167f", "role": "user", "content": "best portable LED light with AA or AAA batteries?", ...}], "pagination": {...}}
HTTP 200
```

Unknown session ids on `/api/sessions/*` are `404 session_not_found` (see §9); an unknown id on
`/v1/runs` is *adopted*, not refused (see §3). The two rules differ deliberately: a session that exists
and belongs to someone else is a conflict, a session that does not exist yet is simply new.

## 11. Attachments — inline images only

The API accepts **inline images** and nothing else. Documents reach the agent as filesystem paths in the
message text, not as uploads.

Accepted, on both the Chat Completions door and the runs door:

```
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"model":"hermes-agent","messages":[{"role":"user","content":[{"type":"text","text":"Reply with only the word image."},{"type":"image_url","image_url":{"url":"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="}}]}]}' \
  http://127.0.0.1:8642/v1/chat/completions
{"id": "chatcmpl-8f5c69dd03394b8fab6cf8a8a03d7", "object": "chat.completion", "created": 1793616088, "model": "hermes-agent", "choices": [{"index": 0, "message": {"role": "assistant", "content": "image"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 20338, "completion_tokens": 3, "total_tokens": 20341}}
HTTP 200

$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"input":[{"role":"user","content":[{"type":"text","text":"Reply with only the word image."},{"type":"image_url","image_url":{"url":"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="}}]}]}' \
  http://127.0.0.1:8642/v1/runs
{"run_id": "run_489e11f527464ee19dc481ab80c13485", "status": "started", "replayed": false}
HTTP 202
```

Rejected — every one is `400` with `code: unsupported_content_type` or `invalid_image_url` and a `param`
pointer to the offending content part:

```
### file part (Chat Completions)
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"model":"hermes-agent","messages":[{"role":"user","content":[{"type":"text","text":"what is in this file?"},{"type":"file","file":{"filename":"notes.txt","file_data":"data:text/plain;base64,aGVsbG8="}}]}]}' \
  http://127.0.0.1:8642/v1/chat/completions
{"error": {"message": "Inline image inputs are supported, but uploaded files and document inputs are not supported on this endpoint.", "type": "invalid_request_error", "param": "messages[0].content", "code": "unsupported_content_type"}}
HTTP 400

### non-image data URL
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"model":"hermes-agent","messages":[{"role":"user","content":[{"type":"text","text":"describe"},{"type":"image_url","image_url":{"url":"data:text/plain;base64,aGVsbG8="}}]}]}' \
  http://127.0.0.1:8642/v1/chat/completions
{"error": {"message": "Only image data URLs are supported. Non-image data payloads are not supported.", "type": "invalid_request_error", "param": "messages[0].content", "code": "unsupported_content_type"}}
HTTP 400

### bare relative path
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"model":"hermes-agent","messages":[{"role":"user","content":[{"type":"text","text":"describe"},{"type":"image_url","image_url":{"url":"./photo.png"}}]}]}' \
  http://127.0.0.1:8642/v1/chat/completions
{"error": {"message": "Image inputs must use http(s) URLs or data:image/... URLs.", "type": "invalid_request_error", "param": "messages[0].content", "code": "invalid_image_url"}}
HTTP 400

### input_file part (Responses API)
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"model":"hermes-agent","input":[{"role":"user","content":[{"type":"input_text","text":"summarise"},{"type":"input_file","file_id":"file-abc123"}]}]}' \
  http://127.0.0.1:8642/v1/responses
{"error": {"message": "Inline image inputs are supported, but uploaded files and document inputs are not supported on this endpoint.", "type": "invalid_request_error", "param": "input[0].content", "code": "unsupported_content_type"}}
HTTP 400
```

Rules for the relay:

- Image parts must be `data:image/...` or `http(s)://` URLs. Anything else — a bare path, a relative
  path, a `data:text/...` payload — is a 400 (the message names the accepted forms: "Image inputs must
  use http(s) URLs or data:image/... URLs").
- A `file` part (Chat Completions) or an `input_file` part (Responses) is always a 400. There is no
  multipart upload for chat.
- `/v1/artifacts/upload` (advertised in `features.browser_extension_control.artifact_transport`) is the
  browser-extension artifact channel (10 MiB, 300 s TTL, JSON/PDF/image/plain-text), not a chat
  attachment path.

## 12. X-Hermes-Session-Key

The declared conversation key is echoed back on admission so the client can confirm what the server
bound:

```
$ curl -sS -i -X POST -H 'Authorization: Bearer $HERMES_API_KEY' -H 'Content-Type: application/json' \
  -H 'X-Hermes-Session-Key: voice-relay-probe' -d '{"input":"Reply with only the word keyed."}' \
  http://127.0.0.1:8642/v1/runs
HTTP/1.1 202 Accepted
X-Hermes-Session-Key: voice-relay-probe
{"run_id": "run_a2fdf7ba9460416683176bff4fb83753", "status": "started", "replayed": false}
```

Semantics verified against the store: a key resolves to the session it was first bound to, so successive
keyed runs continue one conversation. The status of that run reports the bound session, not the run id:

```
$ curl -sS -H 'Authorization: Bearer $HERMES_API_KEY' \
  http://127.0.0.1:8642/v1/runs/run_a2fdf7ba9460416683176bff4fb83753
{"object": "hermes.run", "model": "hermes-agent", "runtime": {"provider": "", "model": "", "route_source": "global"}, "created_at": 1793616161.6675587, "pending_controls": [], "run_id": "run_a2fdf7ba9460416683176bff4fb83753", "status": "completed", "session_id": "run_38984857236e4d79a256816e9238072d", "admission_id": "0c27ded8997e49b4835fcd58047d0a83", "execution_generation": 10, "output": "keyed", "usage": {"input_tokens": 20709, "output_tokens": 4, "total_tokens": 20713}}
```

The relay should send the key and then use the returned `session_id` — never invent a second id for the
same conversation. Note that `execution_generation` keeps advancing as the conversation grows (10 here,
after ten turns of the same key), which is why it must be treated as an opaque value echoed back from
`pending_controls`.

Validation, with exact bodies (300-character key):

```
$ curl -sS -w '\nHTTP %{http_code}\n' -X POST -H 'Authorization: Bearer $HERMES_API_KEY' \
  -H 'Content-Type: application/json' -H 'X-Hermes-Session-Key: kkkk...kkkk' \
  -d '{"input":"hi"}' http://127.0.0.1:8642/v1/runs
{"error": {"message": "Session key too long", "type": "invalid_request_error"}}
HTTP 400
```

That body has **no `param` and no `code`** — the only error in this document shaped like that. Treat a
400 on this header as a client-side bug, not a transient.

A key carrying a NUL byte is rejected by the HTTP parser before the handler runs (curl cannot send one,
so this was probed with a raw socket):

```
HTTP/1.0 400 Bad Request
Content-Type: text/plain; charset=utf-8
Content-Length: 96
Date: Mon, 02 Nov 2026 10:04:45 GMT
Server: Python/3.14 aiohttp/3.14.3
---
Invalid header value char:

  b'X-Hermes-Session-Key: bad\x00key'
                             ^
```

Practical consequence: the implementation's `400 Invalid session key` guard (for CR/LF/NUL) is
unreachable over HTTP — aiohttp rejects those characters first, with a plain-text 400. The relay only has
to handle "too long" (and keep the key printable ASCII).

The header requires API-key auth; with no `API_SERVER_KEY` configured the server answers
`403 X-Hermes-Session-Key requires API key authentication. Configure API_SERVER_KEY to enable this feature.`
— not reachable in this deployment, where a key is mandatory.

## 13. Adapter checklist and failure-mode table

Design rules that follow from the observations above:

- Gate startup on `GET /v1/capabilities`: `run_submission`, `run_events_sse`, `run_stop`,
  `run_approval_response`, `approval_events`, `session_resources`, `session_key_header`.
- Drive turns with `POST /v1/runs` + `GET /v1/runs/{id}/events`. Open the stream immediately after
  admission (the server tolerates a subscriber that arrives before the run registers) and resume with
  `?last_seq=` inside the 300 s buffer window. Use `GET /v1/runs/{id}` only to recover a dropped stream
  or to read the final `output`.
- Bind sessions by `X-Hermes-Session-Key` and read `session_id` back; never assume `run_id == session_id`.
- Check ownership before binding: `GET /api/sessions/{id}` returns `source`; only `api_server` is bindable.
  A `409 permission_denied` from `/v1/runs` means "someone else's session" — do not retry.
- Answer controls with exactly `{request_id, execution_generation, choice}` / `{request_id,
  execution_generation, answer}`; echo the generation from the pending control. Treat
  `approval_not_pending` on a retry as success.
- Poll `pending_controls` — not `status` — to find clarify prompts, since a pending clarify leaves the
  run `running`.
- Expect that with `approvals.mode: smart` many dangerous-class calls resolve with no prompt at all.
- Send images inline (`data:image/...` or `http(s)`); everything else is a 400. Pass document paths in
  the message text.
- On `run.cancelled` with `interrupted: true`, report a user stop, not an error.

Failure-mode table (all observed):

| Call | Condition | Status | `code` |
|---|---|---|---|
| any | missing/bad bearer | 401 | `gateway_auth_failed` |
| `POST /v1/runs` | missing/empty `input`, bad JSON | 400 | `null` |
| `POST /v1/runs` | foreign session id | 409 | `permission_denied` |
| `POST /v1/runs` | same Idempotency-Key, different payload | 409 | `idempotency_key_conflict` |
| `GET/POST /v1/runs/{id}/*` | unknown run id | 404 | `run_not_found` |
| `GET /v1/runs/{id}/events` | buffer swept (300 s) | 404 | `run_not_found` |
| `POST .../approval` | extra/missing key | 400 | `invalid_params` |
| `POST .../approval` | wrong generation | 409 | `stale_generation` |
| `POST .../approval` | unknown or already-answered prompt | 409 | `approval_not_pending` |
| `POST .../clarify` | wrong key set | 400 | `invalid_params` |
| `POST .../clarify` | wrong generation | 409 | `stale_generation` |
| `POST .../clarify` | unknown or already-answered prompt | 409 | `approval_not_pending` |
| `POST /v1/runs` | session key > 256 chars | 400 | *(no code field)* |
| `POST /v1/runs` | session key with NUL/CR/LF | 400 | plain text from aiohttp |
| chat/completions, responses | `file` / `input_file` part, non-image data URL | 400 | `unsupported_content_type` |
| chat/completions | bare/relative image path | 400 | `invalid_image_url` |
| `POST /api/sessions` | duplicate title | 400 | `invalid_title` |
| `POST /api/sessions/{id}/model` | lock without model+provider | 400 | `missing_model` |
| `POST /api/sessions/{id}/chat` | foreign session | 500 | *(plain text, no JSON)* |
| `GET/DELETE /api/sessions/{id}` | unknown id | 404 | `session_not_found` |

## 14. Reproduction

`scripts/probe_hermes_api.sh` produces every transcript in this document. It reads the bearer token from
`HERMES_API_KEY` or from `$HOME/.hermes/.env` (`API_SERVER_KEY`), and for the approval/clarify sections it
temporarily sets `approvals.mode: manual` and enables the `clarify` toolset for `platform_toolsets.api_server`,
restoring the config on exit. Set `HERMES_PROBE_NO_CONFIG=1` to skip both edits (approval and clarify
sections are then skipped).

```bash
HERMES_API_KEY=... scripts/probe_hermes_api.sh 2>&1 | tee /tmp/hermes-api.txt
```

Requirements: the gateway is running with `platforms.api_server.enabled: true` and an `API_SERVER_KEY` of
at least 16 characters; a local model provider; nothing else competing for the gateway's approval channel
while it runs. The probe takes several minutes because every turn is a real model call.

One caveat when editing the config: a duplicate `api_server:` key under `platform_toolsets` makes the
gateway fail to parse its config, and every agent turn then dies with
`terminal policy unavailable for this profile: cannot parse .../config.yaml`. The script edits by line and
refuses to write a duplicate.
