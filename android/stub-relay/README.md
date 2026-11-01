# Stub relay

A Python test fixture that speaks enough of the Pipecat SmallWebRTC/RTVI
signalling contract for the Android scaffold to reach `Connected` and `Ready`.
It is **not** the real Hermes Talk relay.

## What it does

- `GET /health` returns `{"status":"ok"}`.
- `GET /ready` returns `{"status":"ready"}`.
- `POST /start` creates a session and returns an empty ICE server list.
- `POST /sessions/{id}/api/offer` answers an SDP offer after ICE gathering.
- `PATCH /sessions/{id}/api/offer` accepts trickle ICE candidates.
- `DELETE /sessions/{id}` closes the session.
- Opens a `chat` data channel, logs `client-ready`, and sends the RTVI
  `bot-ready` message when the channel opens.
- Optionally sends a 440 Hz tone to the client and counts incoming audio frames.
- Returns JSON 404 for unknown paths and JSON 401 when a token is configured and
  the bearer token does not match.

## What it does not do

- No STT, TTS, LLM, transcript or turn detection.
- No authentication beyond the optional bearer token.
- No TLS. Plain HTTP only.
- No production relay behaviour (no bot lifecycle, no metrics, no reconnection
  policy).

## Run it

From the repository root, using the project virtualenv:

```sh
.venv/bin/python android/stub-relay/relay.py --port 8790
```

Or with any Python 3.12 environment that has `aiortc` installed:

```sh
python3 -m pip install aiortc
python3 android/stub-relay/relay.py --port 8790
```

Options:

```text
--host 127.0.0.1   bind address (default: loopback)
--port 8790        bind port (default: 8790)
--token <token>    require a bearer token on /start and offers
--tone / --no-tone serve the 440 Hz tone (default: on)
```

When `--token` is omitted the relay prints `auth: disabled` and accepts requests
with or without an Authorization header. The token is never printed.

## Point the Android app at it

The app defaults to `http://127.0.0.1:8790`. On a USB device or emulator, forward
the device's loopback port to the host:

```sh
adb reverse tcp:8790 tcp:8790
```

Then run the stub relay on the host and use `http://127.0.0.1:8790` in the app's
settings screen. If you use a token, pass the same value to `--token` and enter it
in the app. This keeps the development path on loopback and avoids hard-coding a
LAN address.

## Notes

- Bind loopback by default; pass `--host` only when you deliberately want to
  expose the fixture on another interface.
- The relay waits for ICE gathering to complete before answering, so the answer
  carries host candidates and no trickle round-trip is required for loopback.
- It is a test fixture. Do not treat its `/ready` response as proof that the
  production relay's RTVI handshake will succeed.
