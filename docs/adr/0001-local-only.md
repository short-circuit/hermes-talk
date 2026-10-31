# ADR 0001 — Local-only runtime

**Status:** accepted
**Date:** 2026-10-31

## Context

Cloud speech-to-speech APIs offer the best latency and naturalness, but bill per minute and move audio
off-site. The project targets a home lab with a GPU and a local agent stack.

## Decision

No runtime component may depend on an internet service. Speech recognition, the conversational model,
speech synthesis, the agent, and tools all run on local hardware. One-time model downloads are permitted.

## Consequences

- Latency is bounded by local inference, not by a vendor's SLA.
- Model choice is constrained to open weights that fit the available VRAM.
- The cloud full-duplex path (`gpt-live-1`) and hosted realtime APIs are out of scope; they may be added
  later as optional backends behind the same interface.
- CI and tests must not call the network.
