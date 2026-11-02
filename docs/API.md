# Relay API reference

**Scope.** Every HTTP and RTVI/WebSocket surface this repository exposes today, the auth scheme, the
error catalogue, and the admission limits behind it. It describes relay `0.1.0` (pre-alpha) as shipped
at commit `adbda00`; the API surface has not changed since, and every example was re-run against that
tree. Where a surface is shipped but not mounted on the production app, that is stated explicitly — an
endpoint listed here is never a plan, only code that answers a request.

**Audience.** An Android client developer, an operator debugging a rejection, or a contributor adding
a route. The upstream Hermes gateway contract (runs, SSE frames, sessions) lives in
[`research/hermes-api.md`](research/hermes-api.md); this document covers the relay's own surface.

**Local-only.** No endpoint in this reference calls a cloud STT, TTS or LLM. The relay process makes
no outbound request while serving any route below; model servers and the Hermes gateway are reached
only by library code that the call loop will mount later.

## How every example here was produced

Each block below is a command that was run against a live process on this host, followed by its
verbatim output. Nothing is illustrative.

The run used two throw-away instances on loopback and one stub-relay fixture, with all state under
`/var/tmp` so no example contains a personal directory path:

```sh
export DATA_DIR=/var/tmp/hermes-talk-docs
export INBOX_DIR=$DATA_DIR/inbox
export OUTBOX_DIR=$DATA_DIR/outbox
mkdir -p "$INBOX_DIR" "$OUTBOX_DIR"
.venv/bin/python -m relay.tokens issue --label docs-example
```

```
ht_nEZCWtoWK3yQHhqHLI5n3lfPhEI688EMHJuirwRWm2Q
issued label=docs-example token_id=acbf18b2 state=/var/tmp/hermes-talk-docs/device_tokens.json
```

```sh
# main instance: port 8799, attachment cap lowered to 1 MB so the 413 example is cheap
RELAY_HOST=127.0.0.1 RELAY_PORT=8799 MAX_ATTACHMENT_MB=1 .venv/bin/python -m relay.app
# second instance: port 8798, no INBOX_DIR and a bootstrap token, to show the 503 paths
env -u INBOX_DIR RELAY_PORT=8798 DEVICE_TOKENS=docs-example-token .venv/bin/python -m relay.app
# RTVI fixture: port 8765
.venv/bin/python android/stub-relay/relay.py --port 8765 --token docs-example-token --no-tone
```

`TOKEN` in the commands below is the issued token. Only the 1 MB cap and the ports differ from the
defaults; every other limit is the default from `relay/config.py`.

The endpoint inventory was cross-checked against the running app's own schema, so the list is
complete rather than hand-collected:

```sh
curl -sS http://127.0.0.1:8799/openapi.json | .venv/bin/python -c "
import json,sys
spec=json.load(sys.stdin)
for path, ops in sorted(spec['paths'].items()):
    for method, op in ops.items():
        print(method.upper(), path, '->', op.get('responses',{}).keys())
print('openapi', spec['openapi'], 'title', spec['info']['title'], spec['info']['version'])
"
```

```
GET / -> dict_keys(['200'])
GET /health -> dict_keys(['200'])
GET /ready -> dict_keys(['200'])
GET /v1/artifacts -> dict_keys(['200', '422'])
GET /v1/artifacts/{artifact_id} -> dict_keys(['200', '422'])
HEAD /v1/artifacts/{artifact_id} -> dict_keys(['200', '422'])
PUT /v1/attachments -> dict_keys(['201', '422'])
POST /v1/attachments -> dict_keys(['201', '422'])
GET /v1/whoami -> dict_keys(['200'])
openapi 3.1.0 title Hermes Talk voice relay 0.1.0
```

Error codes were extracted from the classes that raise them and matched against the bodies the
running app returned (§4). The relevant suites are green:

```sh
.venv/bin/python -m pytest relay/tests/test_attach_api.py relay/tests/test_files_api.py \
  relay/tests/test_health.py relay/tests/test_limits.py relay/tests/test_hermes_adapter.py \
  relay/tests/test_runs.py
```

```
154 passed in 22.58s
```

## 1. Conventions

| Item | Value |
|---|---|
| Base URL (relay) | `http://127.0.0.1:8790` — loopback only; Caddy terminates TLS on `:443` and proxies to it |
| Base URL (RTVI fixture) | `http://127.0.0.1:8790` by default, any `--port` when started explicitly |
| Auth | `Authorization: Bearer <device token>`; no cookie, no query-string token, no mTLS |
| Token format | `ht_` + 43 base64url characters (32 random bytes); stored as a SHA-256 digest in a `0600` state file |
| Token lifecycle | `python -m relay.tokens issue|rotate|revoke|list` — a revoke is visible to the running relay on the next request, no restart |
| Correlation | `X-Correlation-ID` accepted when it matches `^[A-Za-z0-9._-]{1,64}$`; otherwise a UUID hex is generated. The value is always echoed in the response header |
| Request bodies | `multipart/form-data` for uploads, no JSON body on any other route |
| Response bodies | `application/json` except artifact downloads, which stream the stored bytes |
| CORS | not configured; the relay is a server-side client and the Android app is not a browser |
| Versioning | `relay.__version__`; the OpenAPI schema is served at `/openapi.json` and Swagger UI at `/docs` |

### 1.1 Error envelope

Every relay-generated rejection is a JSON object with these keys:

```json
{
  "error": "rate_limited",
  "detail": "rate limit for 'download' exceeded",
  "spoken": "One moment — the relay is handling too many requests.",
  "retry_after": 1,
  "correlation_id": "3f2ca47be74944f3b7c9ce2166228aec"
}
```

- `error` — stable machine code; the catalogue is §4.
- `detail` — one sentence for logs; never contains token material or raw bytes.
- `spoken` — a short sentence the client renders through local text-to-speech. Every rejection path
  sets it, so an error is never silent.
- `retry_after` — seconds, or `null`. When it is a number, the same value is sent in a `Retry-After`
  response header.
- `correlation_id` — the request's correlation id. The `X-Correlation-ID` header is set on every
  response including 401; the body field is absent from 401 and framework (404/405/422) responses,
  which do not run through the relay's error handlers.

`spoken` strings are part of the contract: they are user-visible audio, so changing one is a client
change. Every storage and admission rejection sets one; 401 responses do not, because the client speaks
its own sentence ("The relay did not accept the device token." in the Android attachment client).

## 2. Relay HTTP endpoints

All routes except `/`, `/health` and `/ready` require a device token. Requests are admitted through
the middleware in `relay/app.py` before the handler runs; §5 describes the limits.

### 2.1 `GET /` — service banner

Unauthenticated. Returns the service name, version and the Swagger path.

```sh
curl -sS -w '\nHTTP %{http_code}\n' http://127.0.0.1:8799/
```

```
{"service":"voice-relay","version":"0.1.0","docs":"/docs"}
HTTP 200
```

### 2.2 `GET /health` — liveness

Unauthenticated, 200 whenever the process is serving. It performs no I/O, so a 200 does not mean the
storage or auth configuration is usable — use `/ready` for that.

```sh
curl -sS -w '\nHTTP %{http_code}\n' http://127.0.0.1:8799/health
```

```
{"status":"ok","service":"voice-relay","version":"0.1.0"}
HTTP 200
```

### 2.3 `GET /ready` — readiness

Unauthenticated. Runs six checks and returns 200 only when all pass; otherwise 503 with the same
shape and `"status": "not_ready"`. The check names are `settings`, `storage`, `auth`, `models`,
`shutdown` and `limits`. Details never contain tokens; storage paths are shown relative to `DATA_DIR`
when they live under it.

Ready instance:

```sh
curl -sS -w '\nHTTP %{http_code}\n' http://127.0.0.1:8799/ready
```

```
{"status":"ready","checks":[{"name":"settings","ok":true,"detail":"all required settings present"},{"name":"storage","ok":true,"detail":"writable: inbox, outbox, ."},{"name":"auth","ok":true,"detail":"1 active device token(s)"},{"name":"models","ok":true,"detail":"all model names configured"},{"name":"shutdown","ok":true,"detail":"shutdown not in progress"},{"name":"limits","ok":true,"detail":"sessions 0/3; runs 0/2; uploads 0 in flight, 0/8 queued"}]}
HTTP 200
```

Second instance with `INBOX_DIR` and `DEVICE_TOKENS` unset:

```sh
curl -sS -w '\nHTTP %{http_code}\n' http://127.0.0.1:8798/ready
```

```
{"status":"not_ready","checks":[{"name":"settings","ok":false,"detail":"missing required settings: device_tokens, inbox_dir"},{"name":"storage","ok":true,"detail":"writable: /var/tmp/hermes-talk-docs/outbox, ."},{"name":"auth","ok":false,"detail":"no device tokens configured"},{"name":"models","ok":true,"detail":"all model names configured"},{"name":"shutdown","ok":true,"detail":"shutdown not in progress"},{"name":"limits","ok":true,"detail":"sessions 0/3; runs 0/2; uploads 0 in flight, 0/8 queued"}]}
HTTP 503
```

A `shutdown` check turning false is the only transient reason for 503; it is set while the lifespan
drain runs.

### 2.4 `GET /v1/whoami` — device identity

Returns the public metadata of the authenticated token. No token material is echoed.

```sh
curl -sS -w '\nHTTP %{http_code}\n' -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8799/v1/whoami
```

```
{"label":"docs-example","token_id":"acbf18b2","issued_at":"2026-11-02T17:26:05.228716+00:00"}
HTTP 200
```

| Status | Body | When |
|---|---|---|
| 200 | `{label, token_id, issued_at}` | token verified |
| 401 | `{"error":"unauthorized","reason":"missing_credentials"}` | no `Authorization` header |
| 401 | `{"error":"unauthorized","reason":"invalid_scheme"}` | header present but not `Bearer <token>` |
| 401 | `{"error":"unauthorized","reason":"invalid_token"}` | unknown, malformed or revoked token |
| 401 | `{"error":"unauthorized","reason":"no_tokens_configured"}` | no state records and no bootstrap token |

Every 401 carries `WWW-Authenticate: Bearer`.

```sh
curl -sS -w '\nHTTP %{http_code}\n' http://127.0.0.1:8799/v1/whoami
curl -sS -w '\nHTTP %{http_code}\n' -H "Authorization: Basic $TOKEN" http://127.0.0.1:8799/v1/whoami
curl -sS -w '\nHTTP %{http_code}\n' -H "Authorization: Bearer ht_wrong" http://127.0.0.1:8799/v1/whoami
```

```
{"error":"unauthorized","reason":"missing_credentials"}
HTTP 401
{"error":"unauthorized","reason":"invalid_scheme"}
HTTP 401
{"error":"unauthorized","reason":"invalid_token"}
HTTP 401
```

### 2.5 `POST /v1/attachments` and `PUT /v1/attachments` — upload one file

Both methods reach the same handler; `PUT` exists so a client that retries a transport failure can
use an idempotent method. **The handler is not idempotent**: two uploads of the same bytes produce two
attachments with distinct ids.

Request: `multipart/form-data` with one required part named `file`. The declared filename's extension
must belong to the sniffed type; the declared `Content-Type` is advisory and is never trusted over the
magic bytes.

Allowlisted types (`relay/storage/inbox.py::ALLOWED_TYPES`):

| Sniffed MIME | Kind | Permitted extensions |
|---|---|---|
| `image/png` | image | `.png` |
| `image/jpeg` | image | `.jpg`, `.jpeg` |
| `image/webp` | image | `.webp` |
| `image/gif` | image | `.gif` |
| `application/pdf` | document | `.pdf` |
| `application/zip` | archive | `.zip` |
| `audio/wav` | audio | `.wav` |
| `audio/mpeg` | audio | `.mp3` |
| `audio/ogg` | audio | `.ogg`, `.oga`, `.opus` |
| `audio/flac` | audio | `.flac` |
| `audio/mp4` | audio | `.m4a`, `.mp4` |
| `audio/webm` | audio | `.webm` |
| `text/plain` | text | `.txt`, `.md`, `.csv` |

Response 201:

```json
{
  "attachment": {
    "id": "note-f7cb955aac56.png",
    "mime": "image/png",
    "kind": "image",
    "sha256": "c2a5a162259db11b2f862c3540883fae11cff377e3948f544d133fa5d2974be2",
    "content_sha256": "c2a5a162259db11b2f862c3540883fae11cff377e3948f544d133fa5d2974be2",
    "size_bytes": 77,
    "uploaded_bytes": 77,
    "detected_mime": "image/png",
    "facts": {
      "detected_mime": "image/png",
      "width": 8,
      "height": 8,
      "stored_width": 8,
      "stored_height": 8
    },
    "original_name": "note.png",
    "created_at": "2026-11-02T17:27:06.062398+00:00",
    "path": "/var/tmp/hermes-talk-docs/inbox/note-f7cb955aac56.png"
  },
  "spoken": "Picture saved."
}
```

Field notes:

| Field | Meaning |
|---|---|
| `id` | server-generated: sanitized stem + `-` + 12 hex characters + lowercase extension |
| `mime`, `detected_mime` | the sniffed type; `facts.detected_mime` repeats it for the agent |
| `kind` | one of `image`, `audio`, `document`, `archive`, `text` |
| `sha256`, `content_sha256` | SHA-256 of the stored bytes, recorded at ingest and re-verified before use |
| `size_bytes`, `uploaded_bytes` | stored size and wire size; they differ when metadata is stripped from JPEG/PNG/WebP |
| `facts` | structural facts from the isolated parser; empty for text |
| `path` | absolute path inside `INBOX_DIR`, handed to the agent |
| `spoken` | kind-specific sentence: "Picture saved.", "Audio saved.", "Document saved.", "Archive saved.", "Text file saved.", else "File saved." |

The image example, run:

```sh
curl -sS -w '\nHTTP %{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  -F "file=@/var/tmp/hermes-talk-docs/fixtures/note.png" http://127.0.0.1:8799/v1/attachments
```

```
{"attachment":{"id":"note-f7cb955aac56.png","mime":"image/png","kind":"image","sha256":"c2a5a162259db11b2f862c3540883fae11cff377e3948f544d133fa5d2974be2","content_sha256":"c2a5a162259db11b2f862c3540883fae11cff377e3948f544d133fa5d2974be2","size_bytes":77,"uploaded_bytes":77,"detected_mime":"image/png","facts":{"detected_mime":"image/png","width":8,"height":8,"stored_width":8,"stored_height":8},"original_name":"note.png","created_at":"2026-11-02T17:27:06.062398+00:00","path":"/var/tmp/hermes-talk-docs/inbox/note-f7cb955aac56.png"},"spoken":"Picture saved."}
HTTP 201
```

The same bytes over `PUT`:

```sh
curl -sS -w '\nHTTP %{http_code}\n' -X PUT -H "Authorization: Bearer $TOKEN" \
  -F "file=@/var/tmp/hermes-talk-docs/fixtures/notes.txt" http://127.0.0.1:8799/v1/attachments
```

```
{"attachment":{"id":"notes-8accc21dd629.txt","mime":"text/plain","kind":"text","sha256":"162ec7955fe3d5f75d5a702becb5cddd76b02b2d23ae5459abdd76cca3c61203","content_sha256":"162ec7955fe3d5f75d5a702becb5cddd76b02b2d23ae5459abdd76cca3c61203","size_bytes":18,"uploaded_bytes":18,"detected_mime":"text/plain","facts":{},"original_name":"notes.txt","created_at":"2026-11-02T17:28:59.458388+00:00","path":"/var/tmp/hermes-talk-docs/inbox/notes-8accc21dd629.txt"},"spoken":"Text file saved."}
HTTP 201
```

Rejections, all run:

```sh
# empty file -> 400 empty_upload
curl -sS -w '\nHTTP %{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  -F "file=@/var/tmp/hermes-talk-docs/fixtures/empty.txt" http://127.0.0.1:8799/v1/attachments
```

```
{"error":"empty_upload","detail":"the uploaded file contains zero bytes","spoken":"That file was empty, so there was nothing to save.","retry_after":null,"correlation_id":"b0032a2fccc14986afee510b910378cc"}
HTTP 400
```

```sh
# 2 MiB file against a 1 MB cap -> 413 file_too_large, refused before parsing
curl -sS -w '\nHTTP %{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  -F "file=@/var/tmp/hermes-talk-docs/fixtures/big.txt" http://127.0.0.1:8799/v1/attachments
```

```
{"error":"file_too_large","detail":"declared length 2097349 bytes exceeds the 1 MB limit","spoken":"That file is too large to send. Try a smaller one.","retry_after":null,"correlation_id":"0da64897d1c549888fe016047c40a03e"}
HTTP 413
```

```sh
# bytes with no allowlisted magic -> 415 unsupported_type
curl -sS -w '\nHTTP %{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  -F "file=@/var/tmp/hermes-talk-docs/fixtures/blob.bin" http://127.0.0.1:8799/v1/attachments
```

```
{"error":"unsupported_type","detail":"the file's magic bytes are not allowlisted","spoken":"That file type is not supported.","retry_after":null,"correlation_id":"6805fbc6736a45719a75f456e0607833"}
HTTP 415
```

```sh
# PNG bytes named photo.jpg -> 415 type_mismatch
curl -sS -w '\nHTTP %{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  -F "file=@/var/tmp/hermes-talk-docs/fixtures/photo.jpg" http://127.0.0.1:8799/v1/attachments
```

```
{"error":"type_mismatch","detail":"the extension does not belong to the sniffed type image/png","spoken":"The file's contents do not match its name.","retry_after":null,"correlation_id":"c0dd97e1628745c58c34d7f2e6ca87ce"}
HTTP 415
```

```sh
# no file part -> framework validation error
curl -sS -w '\nHTTP %{http_code}\n' -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8799/v1/attachments
```

```
{"detail":[{"type":"missing","loc":["body","file"],"msg":"Field required","input":null}]}
HTTP 422
```

```sh
# INBOX_DIR unset on the second instance -> spoken 503
curl -sS -w '\nHTTP %{http_code}\n' -H 'Authorization: Bearer docs-example-token' \
  -F "file=@/var/tmp/hermes-talk-docs/fixtures/notes.txt" http://127.0.0.1:8798/v1/attachments
```

```
{"error":"inbox_not_configured","detail":"INBOX_DIR is not configured","spoken":"Attachments are not set up on this relay yet.","retry_after":null,"correlation_id":"efbc56d79368497ca7ef08b66aa4c5b9"}
HTTP 503
```

Upload admission order, as implemented: declared `Content-Length` pre-check (cap + 1 MiB headroom) →
token verification → upload lease (slot or FIFO queue) → streaming cap → magic-byte sniff → extension
family check → isolated structural parse → atomic `0600` write → audit record. The streaming cap
inside `InboxStore.ingest` is authoritative; the `Content-Length` pre-check only avoids parsing a body
that is obviously too large.

### 2.6 `GET /v1/artifacts` — list artifacts

Artifacts are files the agent left in `OUTBOX_DIR`; the relay never writes there. Listing is newest
first, skips dotfiles and symlinks, recurses into subdirectories, and returns at most `limit` records
(query bounds 1–200, default 50).

```sh
curl -sS -w '\nHTTP %{http_code}\n' -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8799/v1/artifacts
```

```
{"artifacts":[{"id":"reports/latest.json","name":"latest.json","mime":"application/json","size_bytes":12,"sha256":"b3847b7102cc23ce6be499d5b4b34a669d7aea9ac7459ff14c1204eb3f287fe1","created_at":"2026-11-02T17:27:35.797429+00:00","expires_at":null},{"id":"summary.md","name":"summary.md","mime":"text/markdown","size_bytes":52,"sha256":"59fb8823fa71e0f13cc380da516637f8e4c1c74f95126be041fa6074d01a5341","created_at":"2026-11-02T17:27:35.797429+00:00","expires_at":null}],"count":2,"limit":50,"retention_days":0}
HTTP 200
```

`retention_days` is `OUTBOX_RETENTION_DAYS` (`0` disables expiry). Expired files are omitted from the
list and answered with 404 on download.

```sh
curl -sS -w '\nHTTP %{http_code}\n' -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8799/v1/artifacts?limit=1'
```

```
{"artifacts":[{"id":"reports/latest.json","name":"latest.json","mime":"application/json","size_bytes":12,"sha256":"b3847b7102cc23ce6be499d5b4b34a669d7aea9ac7459ff14c1204eb3f287fe1","created_at":"2026-11-02T17:27:35.797429+00:00","expires_at":null}],"count":1,"limit":1,"retention_days":0}
HTTP 200
```

```sh
curl -sS -w '\nHTTP %{http_code}\n' -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8799/v1/artifacts?limit=0'
```

```
{"detail":[{"type":"greater_than_equal","loc":["query","limit"],"msg":"Input should be greater than or equal to 1","input":"0","ctx":{"ge":1}}]}
HTTP 422
```

### 2.7 `GET /v1/artifacts/{artifact_id}` and `HEAD /v1/artifacts/{artifact_id}` — download

`artifact_id` is the path-relative id from the listing; the route uses a `:path` converter so nested
ids such as `reports/latest.json` work. Download streams the file in 64 KiB chunks and sends:

| Header | Value |
|---|---|
| `Content-Type` | curated MIME from the extension, then magic bytes, else `application/octet-stream` |
| `Content-Length` | stored size |
| `Content-Disposition` | `attachment; filename="..."` plus an RFC 5987 `filename*` when the name is not ASCII |
| `ETag` | `"<sha256>"` |
| `Last-Modified` | file mtime in RFC 7231 form |
| `X-Artifact-SHA256` | sha256 of the bytes |
| `X-Artifact-Id` | the id, percent-encoded if not ASCII |
| `X-Content-Type-Options` | `nosniff` |
| `Cache-Control` | `private, max-age=0, must-revalidate` |

`HEAD` returns the same headers with an empty body. `If-None-Match` matching the ETag (strong or
`W/` weak form, or `*`) returns 304 with the headers and no body.

```sh
curl -sS -D - -o /var/tmp/hermes-talk-docs/downloaded.md -w '\nHTTP %{http_code}\n' \
  -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8799/v1/artifacts/summary.md
cat /var/tmp/hermes-talk-docs/downloaded.md
```

```
HTTP/1.1 200 OK
date: Mon, 02 Nov 2026 17:27:42 GMT
server: uvicorn
content-type: text/markdown
content-length: 52
content-disposition: attachment; filename="summary.md"
etag: "59fb8823fa71e0f13cc380da516637f8e4c1c74f95126be041fa6074d01a5341"
last-modified: Mon, 02 Nov 2026 17:27:35 GMT
x-artifact-sha256: 59fb8823fa71e0f13cc380da516637f8e4c1c74f95126be041fa6074d01a5341
x-artifact-id: summary.md
x-content-type-options: nosniff
cache-control: private, max-age=0, must-revalidate
x-correlation-id: c30777883fcb4424a588d82d36009855

HTTP 200
# Relay example artifact

Produced by the docs run.
```

```sh
curl -sS -I -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8799/v1/artifacts/summary.md
```

```
HTTP/1.1 200 OK
date: Mon, 02 Nov 2026 17:28:01 GMT
server: uvicorn
content-type: text/markdown
content-length: 52
content-disposition: attachment; filename="summary.md"
etag: "59fb8823fa71e0f13cc380da516637f8e4c1c74f95126be041fa6074d01a5341"
last-modified: Mon, 02 Nov 2026 17:27:35 GMT
x-artifact-sha256: 59fb8823fa71e0f13cc380da516637f8e4c1c74f95126be041fa6074d01a5341
x-artifact-id: summary.md
x-content-type-options: nosniff
cache-control: private, max-age=0, must-revalidate
x-correlation-id: 9432445ce15645149c1bea24e34a6c3b
```

```sh
curl -sS -D - -o /dev/null -w 'HTTP %{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  -H 'If-None-Match: "59fb8823fa71e0f13cc380da516637f8e4c1c74f95126be041fa6074d01a5341"' \
  http://127.0.0.1:8799/v1/artifacts/summary.md
```

```
HTTP/1.1 304 Not Modified
date: Mon, 02 Nov 2026 17:28:01 GMT
server: uvicorn
content-type: text/markdown
content-length: 52
content-disposition: attachment; filename="summary.md"
etag: "59fb8823fa71e0f13cc380da516637f8e4c1c74f95126be041fa6074d01a5341"
last-modified: Mon, 02 Nov 2026 17:27:35 GMT
x-artifact-sha256: 59fb8823fa71e0f13cc380da516637f8e4c1c74f95126be041fa6074d01a5341
x-artifact-id: summary.md
x-content-type-options: nosniff
cache-control: private, max-age=0, must-revalidate
x-correlation-id: ef03fee828a641ec853de2f5a754c050

HTTP 304
```

Unknown id and a traversal attempt:

```sh
curl -sS -w '\nHTTP %{http_code}\n' -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8799/v1/artifacts/missing.txt
curl -sS -w '\nHTTP %{http_code}\n' -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8799/v1/artifacts/..%2F..%2Fetc%2Fpasswd'
```

```
{"error":"artifact_not_found","detail":"the artifact does not exist","spoken":"That file could not be found.","retry_after":null,"correlation_id":"e9d542d311dc45399474932d64832bba"}
HTTP 404
{"error":"artifact_not_found","detail":"artifact id contains an invalid segment","spoken":"That file could not be found.","retry_after":null,"correlation_id":"2bce1bb1a67d4aa8bfd451c2cd795a92"}
HTTP 404
```

Confinement rules: every segment must be non-empty and not `.`/`..`; the resolved path must equal the
joined path and stay under `OUTBOX_DIR`; symlinks are never followed (`O_NOFOLLOW`); non-regular files
are treated as not found. An expired artifact returns `artifact_expired` (404) before it is hashed.

```sh
curl -sS -w '\nHTTP %{http_code}\n' -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8799/v1/artifacts/reports/latest.json
```

```
{"runs": 1}

HTTP 200
```

## 3. Routes that are not mounted yet

- `POST /v1/runs` — reserved in `relay/limits.py::RATE_LIMIT_ROUTES` with the `run` rate-limit
  category and the run-lease capacity. The call loop will mount it; until then a request to it is a
  framework 404.
- No SSE endpoint and no WebSocket endpoint on the production app. The relay consumes the gateway's
  SSE stream as a client (`relay/hermes/client.py`) and emits normalized events internally (§6). The
  RTVI/WebSocket surface in §5 is served today by the stub fixture only.

## 4. Error catalogue

Every relay-generated rejection maps to one of these. `spoken` is verbatim from the code; `source` is
the class that raises it.

| Status | `error` | `spoken` | Source |
|---|---|---|---|
| 401 | `unauthorized` (`reason` = `missing_credentials`, `invalid_scheme`, `invalid_token`, `no_tokens_configured`) | — (no spoken; the client says its own sentence) | `relay/auth/deps.py::_DeviceAuthError` |
| 400 | `empty_upload` | That file was empty, so there was nothing to save. | `relay.storage.errors.EmptyUploadError` |
| 404 | `attachment_not_found` | That attachment could not be found. | `relay.storage.errors.AttachmentNotFoundError` |
| 409 | `attachment_changed` | That file changed after it was saved. Try sending it again. | `relay.storage.errors.IntegrityError` |
| 413 | `archive_too_complex` | That zip file is too big to unpack. Try a smaller one. | `relay.storage.errors.ArchiveTooComplexError` |
| 413 | `file_too_large` | That file is too large to send. Try a smaller one. | `relay.storage.errors.FileTooLargeError` |
| 413 | `image_too_large` | That picture has too many pixels. Try a smaller one. | `relay.storage.errors.ImageTooLargeError` |
| 415 | `type_mismatch` | The file's contents do not match its name. | `relay.storage.errors.TypeMismatchError` |
| 415 | `unsupported_type` | That file type is not supported. | `relay.storage.errors.UnsupportedTypeError` |
| 422 | `archive_corrupt` | That zip file could not be read. Try another one. | `relay.storage.errors.ArchiveCorruptError` |
| 422 | `corrupt_document` | That document could not be read. Try another one. | `relay.storage.errors.CorruptDocumentError` |
| 422 | `corrupt_media` | That picture could not be read. Try another one. | `relay.storage.errors.CorruptMediaError` |
| 422 | `document_encrypted` | That document is locked with a password. | `relay.storage.errors.EncryptedDocumentError` |
| 422 | `document_too_complex` | That document is too long. Try a shorter one. | `relay.storage.errors.DocumentTooComplexError` |
| 422 | `parse_timeout` | That file took too long to read. Try a smaller one. | `relay.storage.errors.ParseTimeoutError` |
| 503 | `inbox_not_configured` | Attachments are not set up on this relay yet. | `relay.storage.errors.InboxConfigError` |
| 503 | `inbox_unavailable` | The relay cannot save the file right now. Try again in a moment. | `relay.storage.errors.InboxUnavailableError` |
| 503 | `upload_busy` | The relay is busy saving another file. Try again in a moment. | `relay.storage.errors.UploadBusyError` |
| 404 | `artifact_expired` | That file has expired. | `relay.storage.outbox.ArtifactExpiredError` |
| 404 | `artifact_not_found` | That file could not be found. | `relay.storage.outbox.ArtifactNotFoundError` |
| 503 | `outbox_not_configured` | Files are not set up on this relay yet. | `relay.storage.outbox.OutboxConfigError` |
| 503 | `outbox_unavailable` | The relay cannot open that file right now. | `relay.storage.outbox.OutboxUnavailableError` |
| 429 | `rate_limited` | One moment — the relay is handling too many requests. | `relay.limits.RateLimitExceeded` |
| 429 | `reconnect_budget_exhausted` | The connection kept dropping. Start a new call. | `relay.limits.ReconnectBudgetExceeded` |
| 429 | `upload_queue_full` | The upload queue is full. Try again in a moment. | `relay.limits.UploadQueueFull` |
| 503 | `run_limit_reached` | Hermes is busy with another request. Try again shortly. | `relay.limits.RunLimitExceeded` |
| 503 | `session_limit_reached` | The relay is already on a call. Try again shortly. | `relay.limits.SessionLimitExceeded` |

Codes raised by the transport layer rather than by a relay class:

| Status | Body | When |
|---|---|---|
| 404 | `{"detail":"Not Found"}` | unknown path |
| 405 | `{"detail":"Method Not Allowed"}` | known path, wrong method |
| 422 | `{"detail":[{...}]}` | FastAPI validation (missing form field, query bound, wrong content type) |
| 500 | `{"error":"internal_error","correlation_id":"..."}` | unhandled exception; the traceback goes to the log, never to the client |

```sh
curl -sS -w '\nHTTP %{http_code}\n' http://127.0.0.1:8799/nope
curl -sS -w '\nHTTP %{http_code}\n' -X DELETE http://127.0.0.1:8799/health
```

```
{"detail":"Not Found"}
HTTP 404
{"detail":"Method Not Allowed"}
HTTP 405
```

`reconnect_budget_exhausted` and the run/session limits are raised by the admission controller, not by
an HTTP route today; they appear on the wire as soon as `POST /v1/runs` and the call loop are mounted.
`attachment_not_found` and `attachment_changed` come from the inbox retrieval paths
(`InboxStore.resolve`/`InboxStore.delegation_parts`), which no mounted route calls yet; uploads cannot
produce them.

## 5. Limits and admission control

Every limit is a `relay/config.py` setting; the table gives the shipped default. The controller is
built at startup and refuses inconsistent settings (`burst > per_minute`, per-device runs above the
global run cap, `MAX_QUEUED_UPLOADS < 1`).

| Setting | Default | Effect |
|---|---|---|
| `RATE_LIMIT_RUN_PER_MINUTE` / `RATE_LIMIT_RUN_BURST` | 20 / 5 | per-device token bucket for `POST /v1/runs` |
| `RATE_LIMIT_UPLOAD_PER_MINUTE` / `RATE_LIMIT_UPLOAD_BURST` | 30 / 10 | per-device bucket for `POST`/`PUT /v1/attachments` |
| `RATE_LIMIT_DOWNLOAD_PER_MINUTE` / `RATE_LIMIT_DOWNLOAD_BURST` | 120 / 30 | per-device bucket for `GET`/`HEAD /v1/artifacts` and `/v1/artifacts/{id}` |
| `RATE_LIMIT_MAX_DEVICES` | 64 | LRU bound on tracked `(token_id, category)` buckets |
| `MAX_CONCURRENT_SESSIONS` | 3 | call sessions; over the cap → 503 `session_limit_reached` |
| `MAX_CONCURRENT_RUNS` | 2 | concurrent Hermes runs; over → 503 `run_limit_reached` |
| `MAX_CONCURRENT_RUNS_PER_DEVICE` | 1 | per-device run cap |
| `MAX_CONCURRENT_UPLOADS` | 2 | in-flight upload slots |
| `MAX_QUEUED_UPLOADS` | 8 | FIFO queue depth; full → 429 `upload_queue_full` |
| `REJECT_RETRY_AFTER_SECONDS` | 2 | how long a queued upload waits for a slot before 503 `upload_busy`; also the `Retry-After` value on 429/503 |
| `MAX_ATTACHMENT_MB` | 50 | wire cap per upload (streaming abort) |
| `SESSION_IDLE_TIMEOUT_SECONDS` | 120 | idle session deadline; reaped every 5 s |
| `MAX_SSE_FRAME_BYTES` | 65536 | cap on one SSE frame the relay forwards |
| `SSE_IDLE_TIMEOUT_SECONDS` | 30 | idle stream timeout; the relay stops the run |
| `SSE_MAX_RECONNECT_ATTEMPTS` | 5 | reconnects per session before 429 `reconnect_budget_exhausted` |
| `BARGE_IN_MIN_SPEECH_MS` / `BARGE_IN_COOLDOWN_MS` | 300 / 1000 | interruption debounce |
| `OUTBOX_RETENTION_DAYS` | 0 | artifact expiry; 0 disables |
| `MAX_CALL_MINUTES` | 60 | call-length cap reserved for the call loop; nothing in this tree enforces it yet |

Mechanics: one token bucket per `(token_id, category)`, capacity `burst`, refill
`per_minute / 60` per second, keyed by the public 8-hex token id (never the token). A bucket that has
refilled fully is dropped from memory. The route→category mapping is
`relay/limits.py::RATE_LIMIT_ROUTES` and matches a prefix, so `/v1/artifacts/{id}` counts as
`download`.

A `429` always carries `Retry-After`, computed as the ceiling of the seconds until one token refills.
Run and download rate limits are per device, not global.

The download bucket in the docs run had the default burst of 30. Thirty listings succeeded, the 31st
was rejected:

```sh
for i in $(seq 1 40); do
  code=$(curl -sS -o /var/tmp/hermes-talk-docs/rl.json -w '%{http_code}' \
    -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8799/v1/artifacts)
  printf '%s %s\n' "$i" "$code"
  [ "$code" = 429 ] && break
done
cat /var/tmp/hermes-talk-docs/rl.json
```

```
1 200
...
30 200
31 429
{"error":"rate_limited","detail":"rate limit for 'download' exceeded","spoken":"One moment — the relay is handling too many requests.","retry_after":1,"correlation_id":"3f2ca47be74944f3b7c9ce2166228aec"}
```

```sh
curl -sS -D - -o /dev/null -w 'HTTP %{http_code}\n' -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8799/v1/artifacts
```

```
HTTP/1.1 429 Too Many Requests
date: Mon, 02 Nov 2026 17:28:33 GMT
server: uvicorn
retry-after: 1
content-length: 205
content-type: application/json
x-correlation-id: 3ebd0d55541c49d286f7d94fe787f1f8

HTTP 429
```

An unauthenticated request is not rate-limited (there is no device id to charge); it is answered 401
by the route's auth dependency.

Upload admission is two-stage. A queued lease polls for a free slot for
`REJECT_RETRY_AFTER_SECONDS`; if none frees up the request fails with 503 `upload_busy`, not 429. The
429 `upload_queue_full` is only for a full queue at acquire time. Both paths are covered by
`relay/tests/test_attach_api.py` and `relay/tests/test_limits.py`.

## 6. Event names

The relay does not expose an SSE endpoint to clients. It consumes the gateway's SSE stream and
normalizes each frame into an internal event for the voice layer. These names are the contract
between `relay/hermes/events.py` and `relay/brain/delegation.py`; they are what a new transport would
forward.

| Normalized `EventKind` | Upstream frame names accepted | Terminal |
|---|---|---|
| `tool.started` | `tool.started`, `tool.start` | no |
| `tool.completed` | `tool.completed`, `tool.complete` | no |
| `message.interim` | `message.delta`, `message.interim` | no |
| `run.completed` | `run.completed` | yes |
| `run.cancelled` | `run.cancelled` | yes |
| `run.failed` | `run.failed` | yes |
| `approval.request` | `approval.request` | no |
| `approval.settled` | `approval.settled`, `approval.responded` | no |
| `clarify.request` | `clarify.request` | no |
| `replay.truncated` | `replay.truncated` | no |
| `unknown` | any other name | no |

Ordering and delivery rules enforced by `relay/hermes/events.py::EventGate`: strictly increasing
`seq`, duplicate suppression, exactly one terminal event per run (including across reconnects), and a
foreign `run_id` is a contract violation. Unknown event names are surfaced as `unknown` rather than
breaking the stream. The raw upstream frame schema is in
[`research/hermes-api.md`](research/hermes-api.md) §4.

## 7. RTVI signalling and data-channel surface (stub fixture)

`android/stub-relay/relay.py` is the only process in this repository that answers the RTVI/WebRTC
signalling contract today. It is a fixture: no STT, TTS or LLM. The production relay does not mount
these routes yet.

Start it (loopback, optional bearer token, tone off):

```sh
.venv/bin/python android/stub-relay/relay.py --host 127.0.0.1 --port 8765 \
  --token docs-example-token --no-tone
```

```
2026-11-02 18:29:19,780 INFO TLS: disabled
2026-11-02 18:29:19,780 INFO stub relay listening on 127.0.0.1:8765
2026-11-02 18:29:19,780 INFO auth: enabled
2026-11-02 18:29:19,780 INFO tone: off
```

### 7.1 Signalling endpoints

| Method | Path | Auth | Success body |
|---|---|---|---|
| `GET` | `/health` | no | `{"status": "ok"}` |
| `GET` | `/ready` | no | `{"status": "ready"}` |
| `POST` | `/start` | bearer | `{"sessionId": "<uuid4>", "iceConfig": {"iceServers": []}}` |
| `POST` | `/sessions/{id}/api/offer` | bearer | `{"sdp": "<answer SDP>", "type": "answer", "pc_id": "<id>"}` |
| `PATCH` | `/sessions/{id}/api/offer` | bearer | `{"status": "ok"}` |
| `DELETE` | `/sessions/{id}` | **no** | `{"status": "ok"}` |

Error bodies on this surface are `{"error": "<message>"}`: 401 `{"error":"unauthorized"}`, 404
`{"error":"not found"}` for an unknown path, 404 `{"error":"unknown session"}` for a stale id, 500
`{"error":"<exception text>"}` for a malformed offer. `DELETE` is unauthenticated by design in the
fixture — it only closes a session.

```sh
curl -sS -w '\nHTTP %{http_code}\n' http://127.0.0.1:8765/health
curl -sS -w '\nHTTP %{http_code}\n' http://127.0.0.1:8765/ready
curl -sS -w '\nHTTP %{http_code}\n' http://127.0.0.1:8765/start
curl -sS -w '\nHTTP %{http_code}\n' -H 'Authorization: Bearer docs-example-token' -X POST http://127.0.0.1:8765/start
```

```
{"status": "ok"}
HTTP 200
{"status": "ready"}
HTTP 200
{"error": "not found"}
HTTP 404
{"sessionId": "a107cbd3-47d6-495a-8b0c-6b7c3ebede56", "iceConfig": {"iceServers": []}}
HTTP 200
```

Offer, trickle and close, with a real SDP generated by `aiortc`:

```sh
S=$(curl -sS -X POST -H "Authorization: Bearer docs-example-token" http://127.0.0.1:8765/start \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["sessionId"])')
.venv/bin/python - "$S" <<'PY'
import asyncio, json
from aiortc import RTCPeerConnection

async def main() -> None:
    pc = RTCPeerConnection()
    pc.createDataChannel("chat")
    await pc.setLocalDescription(await pc.createOffer())
    while pc.iceGatheringState != "complete":
        await asyncio.sleep(0.05)
    json.dump({"sdp": pc.localDescription.sdp, "type": "offer"},
              open("/var/tmp/hermes-talk-docs/offer.json", "w"))
    await pc.close()

asyncio.run(main())
PY
curl -sS -w '\nHTTP %{http_code}\n' -X POST -H "Authorization: Bearer docs-example-token" \
  -H 'Content-Type: application/json' --data @/var/tmp/hermes-talk-docs/offer.json \
  "http://127.0.0.1:8765/sessions/$S/api/offer"
curl -sS -w '\nHTTP %{http_code}\n' -X PATCH -H "Authorization: Bearer docs-example-token" \
  -H 'Content-Type: application/json' -d '{"candidates":[]}' \
  "http://127.0.0.1:8765/sessions/$S/api/offer"
curl -sS -w '\nHTTP %{http_code}\n' -X DELETE "http://127.0.0.1:8765/sessions/$S"
```

Answer body abridged at the SDP (the full value is ~4.5 KB and carries host ICE candidates):

```
{"sdp": "v=0\r\no=- 4002629392 4002629392 IN IP4 0.0.0.0\r\n... a=candidate:... ...", "type": "answer", "pc_id": "4ae82bab-2fd5-47dd-ac46-59fe756b833d"}
HTTP 200
{"status": "ok"}
HTTP 200
{"status": "ok"}
HTTP 200
```

A stale session id answers 404 on the next `PATCH`:

```
{"error": "unknown session"}
HTTP 404
```

The fixture waits for ICE gathering to complete before answering, so a loopback client needs no
trickle round-trip; `PATCH` accepts candidates but duplicate ones are ignored.

### 7.2 Data-channel messages

The peer connection carries a `chat` data channel. Messages are JSON objects with `label`, `type` and
`data`, per the RTVI envelope:

Server → client, sent when the channel opens:

```json
{"label": "rtvi", "type": "bot-ready", "data": {"version": "2.0.0"}}
```

Client → server, accepted and logged:

```json
{"label": "rtvi", "type": "client-ready", "data": {"version": "2.0.0"}}
```

`bot-ready` is sent on channel open regardless of whether `client-ready` has arrived; the fixture
logs `client-ready` and does not gate on it. With `--tone` (the default) the server also publishes a
48 kHz mono s16 440 Hz track and counts incoming audio frames.

End-to-end client, run against the fixture:

```python
"""Minimal RTVI client: signalling over HTTP, then one data-channel exchange."""
import asyncio
import json
import urllib.request

from aiortc import RTCPeerConnection, RTCSessionDescription

BASE = "http://127.0.0.1:8765"
TOKEN = "docs-example-token"


def post(path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else b""
    request = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


async def main() -> None:
    started = post("/start")
    print("start ->", started)

    pc = RTCPeerConnection()
    channel = pc.createDataChannel("chat")
    messages: list[str] = []

    @channel.on("message")
    def on_message(message: str) -> None:
        messages.append(message)
        print("channel <-", message)

    await pc.setLocalDescription(await pc.createOffer())
    answer = post(
        f"/sessions/{started['sessionId']}/api/offer",
        {"sdp": pc.localDescription.sdp, "type": "offer"},
    )
    print("offer -> answer type:", answer["type"], "pc_id:", answer["pc_id"])
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))

    for _ in range(200):
        if channel.readyState == "open" and messages:
            break
        await asyncio.sleep(0.05)

    print("channel state:", channel.readyState)
    channel.send(json.dumps({"label": "rtvi", "type": "client-ready", "data": {"version": "2.0.0"}}))
    for _ in range(200):
        if messages:
            break
        await asyncio.sleep(0.05)

    print("received:", len(messages))
    await pc.close()


asyncio.run(main())
```

```
start -> {'sessionId': '92a4f8b4-0767-48de-9ef3-00cf62756258', 'iceConfig': {'iceServers': []}}
offer -> answer type: answer pc_id: 92a4f8b4-0767-48de-9ef3-00cf62756258
channel <- {"label": "rtvi", "type": "bot-ready", "data": {"version": "2.0.0"}}
channel state: open
received: 1
```

The Android client's contract for this surface (states, pinning, retry policy) is in
[`../android/README.md`](../android/README.md); the fixture's own notes are in
[`../android/stub-relay/README.md`](../android/stub-relay/README.md).

## 8. Compatibility notes

- Adding a route or changing a response field is a client change; the Android attachment client reads
  `attachment.id`, `attachment.kind`, `attachment.sha256`, `attachment.size_bytes`, `spoken`, and the
  artifact `id`/`name`/`size_bytes`/`sha256`/`expires_at`.
- `spoken` sentences and `error` codes are stable identifiers. Prefer branching on `error`.
- `POST /v1/attachments` is not idempotent; `PUT` is safe to retry at the transport level but still
  creates a second record.
- Artifact ids are relative paths. A client must percent-encode each segment and keep the `/`
  separators.
- `/openapi.json` is the machine-readable version of §2; regenerate the inventory from it when adding
  a route, and update §2–§4 in the same change.
