# Hermes API contract for the relay

> Status: **draft — observation pass pending.** Statements marked `[source]` are read from the
> Hermes API-server implementation (`gateway/platforms/api_server*.py`, `gateway/session_api*.py`);
> statements marked `[observed]` come with a captured curl transcript in this document.
> The draft is replaced by a fully observed version before this task closes.

Scope: exactly the surface the voice relay needs to drive a Hermes turn, stream its events, stop it,
answer approvals and clarify questions, and keep its session identity stable. Everything here is
loopback-only; the relay never exposes this API beyond the host.

## 0. Transport and auth

- Base URL in examples: `http://127.0.0.1:8642` (bind address is configuration, not a contract).
- Every endpoint except `/health` requires `Authorization: Bearer $HERMES_API_KEY`. `[source]`
- The API server refuses to start without a key of at least 16 characters. `[source]`
- Auth failures return `401` with `{"error": {"message": "Invalid gateway API key (API_SERVER_KEY)", "type": "gateway_auth_error", "code": "gateway_auth_failed"}}`. `[observed]`
- Error envelope for everything else: `{"error": {"message": ..., "type": ..., "param": ..., "code": ...}}`. `[source]`

## 1. Capabilities

`GET /v1/capabilities` returns the feature flags and the endpoint table. The relay must gate on
`features.run_submission`, `run_events_sse`, `run_stop`, `run_approval_response`,
`approval_events` and `session_resources` rather than assume them. `[source]`

TODO(observed): full capabilities body.

## 2. Starting a run

`POST /v1/runs` `[source]`

Request fields the relay uses:

| Field | Meaning |
|---|---|
| `input` | user message (string, or OpenAI-style content array) |
| `session_id` | bind the turn to an existing API session |
| `X-Hermes-Session-Key` header | declared conversation key: stable memory scope without a session id |
| `Idempotency-Key` header | replay-safe retry; a different payload under the same key is `409 idempotency_key_conflict` |

Response: `run_id` + `status: started` (`202`), plus the declared key echo. `[source]`

TODO(observed): request/response transcript.

## 3. Event stream

`GET /v1/runs/{run_id}/events` (SSE) `[source]`

- Frame format: optional `id:` line, `event:` line naming the event, `data:` JSON payload. `[source]`
- Payload shape: `{"event": <name>, "run_id": ..., "timestamp": ...}` plus per-event fields. `[source]`
- Event names the relay consumes: `run.queued`, `message.interim`, `message.delta`,
  `tool.started`, `tool.completed`, `subagent.start`, `subagent.complete`, `approval.request`,
  `approval.responded`, `clarify.request`, `run.steered`, `run.stopping`, `run.interrupted`,
  `run.completed`, `run.failed`, `run.cancelled`. `[source]`
- Terminal events carry `completed`, `partial`, `interrupted` and, when applicable,
  `turn_exit_reason`. `[source]`

TODO(observed): ordered transcript for a tool-using turn; reconnect/last-event-id behaviour.

## 4. Run status and pending controls

`GET /v1/runs/{run_id}` returns `status`, `session_id`, `admission_id`, `execution_generation`,
`output`, `usage` and `pending_controls`. `[source]`

`pending_controls` entries: `{"kind": "approval"|"clarify", "prompt_id", "execution_generation",
"choices"|"question", "timeout_s"?}`. `[source]`

Status vocabulary: `queued`, `running`, `waiting_for_approval`, `stopping`, `completed`, `failed`,
`cancelled`, `unknown`. `[source]`

TODO(observed): status body while an approval is pending.

## 5. Approval and clarify responses

`POST /v1/runs/{run_id}/approval` and `POST /v1/runs/{run_id}/clarify`. `[source]`

Under a canonical gateway the body must be **exactly**
`{"request_id": ..., "execution_generation": <int>, "choice": "once"|"session"|"always"|"deny"}`
for approvals and `{"request_id": ..., "execution_generation": <int>, "answer": ...}` for clarify —
any other key set is `400 invalid_params`, a stale generation is `409 stale_generation`, and an
already-resolved prompt is `409 approval_not_pending`. `[source]`

TODO(observed): both transcripts, including the refusal bodies.

## 6. Stop

`POST /v1/runs/{run_id}/stop` `[source]`

- Running: `{"run_id": ..., "status": "stopping"}`; the run settles as `cancelled`.
- Already terminal: the terminal status body, `200`.
- Not owned by this process: `409 run_not_active`.

TODO(observed): stop transcript with latency.

## 7. Sessions

`/api/sessions` family: `GET /api/sessions`, `POST /api/sessions`, `GET /api/sessions/{id}`,
`PATCH /api/sessions/{id}`, `DELETE /api/sessions/{id}`, `GET /api/sessions/{id}/messages`,
`POST /api/sessions/{id}/chat`, `POST /api/sessions/{id}/chat/stream`,
`POST /api/sessions/{id}/fork`, `POST /api/sessions/{id}/model`. `[source]`

TODO(observed): list/create/chat/messages/delete transcripts.

## 8. Surface ownership (the 409)

A REST request may only bind a session whose stored `source` is `api_server`. Binding a session
owned by another surface (CLI, Telegram, Kanban, …) is refused with `409 permission_denied`. `[source]`

TODO(observed): transcript, with the foreign session id discovered via sqlite.

## 9. Attachments

Inline images only: `image_url` parts with `data:image/...` or `http(s)://` URLs. `[source]`

- A `file` part (Chat Completions) or `input_file` part (Responses) is `400 unsupported_content_type`.
- A non-image data URL is `400 unsupported_content_type`.
- A relative or bare path is `400 invalid_image_url`.

Documents reach the agent as filesystem paths handed over in the message text, not as uploads. `[source]`

TODO(observed): rejection transcripts.

## 10. Adapter notes for the relay

- Prefer `session_id` + `X-Hermes-Session-Key` together: the key declares the conversation, the id
  pins the row; the key is validated (control characters and >256 characters are refused with
  `400 Invalid session key` / `400 Session key too long`). `[source]`
- Never reuse a session id across surfaces.
- Poll `GET /v1/runs/{id}` only to recover a dropped stream; the SSE stream is the primary path.
- Approval defaults to deny; on timeout the run continues with the deny branch. `[source]`

TODO(observed): session-key echo transcript, invalid key transcript.
