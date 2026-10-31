# Roadmap

| Phase | Outcome | Exit criteria |
|---|---|---|
| P0 | Ground truth | API contract documented; model benchmark run; Android stack chosen; threat model reviewed |
| P1 | Text relay | prompt → agent → streamed answer over WebSocket; stop works |
| P2 | Voice loop | 20 scripted utterances under 1.2 s perceived latency; barge-in works |
| P3 | Delegation | spoken tool narration; destructive command denied on silence |
| P4 | Attachments | image edit and PDF analysis end-to-end |
| P5 | Android | screen-off call, wake word, 30-minute session without drop |
| P6 | Release | docs, CI, deploy draft, security review, v0.1 checklist |

Each phase maps to a kanban epic. Tasks are gated on real dependencies; nothing runs before its inputs
exist.
