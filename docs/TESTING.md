# Testing

## Layers

- **Unit** — parsers, event mapping, attachment validation, approval policy.
- **Integration** — relay against a stub Hermes API (recorded SSE fixtures); no real agent needed.
- **End-to-end** — relay + Hermes + local models, driven by scripted audio fixtures.
- **Android** — instrumentation tests for the call state machine; emulator for UI flows.
- **Soak** — 30-minute call with induced network loss and reconnection.

## Fixtures

Synthetic only. Use short generated tones or a TTS-generated corpus; never commit personal recordings.
Keep fixtures under 2 MB.

## Latency SLOs

| Metric | Target |
|---|---|
| Endpointing | 300–500 ms |
| STT final | < 300 ms |
| Brain first token | < 300 ms |
| TTS first audio | < 250 ms |
| Perceived response | < 1.2 s p50 |

Failures are reported with the measured distribution, not a single sample.
