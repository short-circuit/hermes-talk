# voice-relay

Python service that owns the WebRTC leg, the local speech pipeline, the voice-brain model, and the Hermes
adapter.

Planned layout:

```
relay/
  app.py            # entrypoint, config loading
  transport/        # SmallWebRTC offer/answer, audio frames
  speech/           # VAD, STT, TTS adapters
  brain/            # LFM tool router, prompts, tool schemas
  hermes/           # runs/SSE adapter, approvals, stop
  storage/          # inbox/outbox, validation
  tests/
```

Nothing in this module may call the internet at runtime.
