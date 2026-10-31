# Architecture

## Components

1. **Android client** — Kotlin/Compose, RTVI/WebRTC client, on-device wake word, foreground microphone
   service, attachment picker, artifact viewer.
2. **voice-relay** — Python service. Owns the WebRTC leg, the speech pipeline, the conversational model,
   and the Hermes adapter.
3. **Hermes API server** — OpenAI-compatible endpoint on loopback. Runs agent turns, streams tool events,
   and exposes approval/clarify/stop controls.
4. **Inference host** — serves the agent model and the local speech models. Reachable only from the relay.

## Data flow

```
phone --WebRTC--> relay --HTTP/SSE--> Hermes --HTTP--> inference
                    |
                    +-- inbox/outbox on local disk
```

## Contracts

### Relay → Hermes

- `POST /v1/runs` with `session_id` and `X-Hermes-Session-Key` for stable memory scoping.
- `GET /v1/runs/{id}/events` (SSE): `tool.started`, `tool.completed`, `message.interim`, `run.completed`.
- `POST /v1/runs/{id}/stop` on barge-in.
- `GET /v1/runs/{id}` → `pending_controls` → `POST /v1/runs/{id}/approval`.
- `POST /v1/runs/{id}/clarify` for agent questions.

The OpenAI-compatible API accepts inline images only; documents are passed as filesystem paths from the
relay inbox.

### Voice brain tools

Four tools, deliberately small:

| Tool | Behaviour |
|---|---|
| `delegate_to_hermes` | enqueue a Hermes turn, return immediately |
| `answer_direct` | reply without the agent |
| `attach_file` | attach a file the user just sent |
| `end_call` | end the conversation |

## Invariants

- No runtime internet dependency.
- The voice brain never holds Hermes' full toolset.
- Attachments are stored outside the repository.
- Approval defaults to deny.
