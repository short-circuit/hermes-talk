# voice-relay

Python service that owns the WebRTC leg, the local speech pipeline, the voice-brain model, and the Hermes
adapter.

Current layout (service shell):

```
relay/
  __init__.py       # package version
  app.py            # FastAPI app: lifespan, correlation ids, /health, /ready
  config.py         # typed settings loaded from the environment
  logging.py        # structured JSON logging, redaction, access log
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
