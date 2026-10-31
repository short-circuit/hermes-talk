# Deployment draft

## Hosts

| Service | Bind | Exposure |
|---|---|---|
| Hermes API server | `127.0.0.1:8642` | loopback only |
| voice-relay | `0.0.0.0:8790` | LAN, TLS via reverse proxy |
| local speech/inference | `127.0.0.1:8081` | loopback only |

## Environment

All configuration comes from `.env` (see `.env.example`). No secrets in compose files or images.

## Compose (draft)

```yaml
services:
  voice-relay:
    build: ../relay
    network_mode: host
    env_file: .env
    volumes:
      - ${INBOX_DIR}:/inbox
      - ${OUTBOX_DIR}:/outbox
    restart: unless-stopped
```

## Reverse proxy (draft)

```
voice.example.lan {
    tls internal
    reverse_proxy 127.0.0.1:8790
}
```

## Rollback

Keep the previous image tag; redeploy with `docker compose up -d --no-build` after retagging. The relay is
stateless apart from inbox/outbox, so rollback does not lose transcripts.
