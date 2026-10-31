# ADR 0002 — WebRTC transport and relay framework

**Status:** proposed

## Context

The Android client needs low-latency bidirectional audio with echo cancellation, plus a server-side
pipeline that can run VAD, STT, an LLM with tool calls, and streaming TTS.

## Options

1. **Pipecat + SmallWebRTC** — one Python process, direct peer-to-peer WebRTC, Kotlin client SDK,
   frame-level control over interruptions. No SFU.
2. **LiveKit Agents + self-hosted SFU** — mature Android starter app, audio turn detector, MCP tool
   support, reconnection; requires an SFU, token server, and TURN.

## Decision

Proposed: Pipecat + SmallWebRTC for v1, because the deployment is single-user on a LAN and the extra SFU
adds operational surface without a benefit at this scale. Revisit if multi-device, telephony, or remote
participants become requirements.

## Consequences

- No SFU to operate; NAT traversal relies on the LAN or a tailnet.
- We own turn-taking logic and must benchmark it.
- Migration to LiveKit later is possible because the Hermes adapter and voice-brain interface are
  transport-independent.
