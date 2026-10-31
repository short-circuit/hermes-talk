# Privacy and data flow

## Runtime

| Data | Where it goes | Leaves the LAN? |
|---|---|---|
| Microphone audio | relay process (memory only unless recording is enabled) | no |
| Transcripts | relay → Hermes session database on the relay host | no |
| Attachments | relay inbox directory | no |
| Agent output / artifacts | relay outbox → phone | no |
| Telemetry | none | no |

The only optional exception is a tailnet coordination service if you use Tailscale to reach the relay from
outside your home network. That service sees connection metadata, not content. On the LAN, the app talks
to the relay directly.

## Repository

The repository is public. It must never contain:

- home-directory paths (`/home/<user>/...`) — use `$HOME`, `${DATA_DIR}`, or documented defaults
- personal email addresses — use `noreply@example.com` or a GitHub noreply address
- machine names, LAN IPs, or hostnames — use `127.0.0.1` or TEST-NET example addresses
- API keys, tokens, passwords, certificates, or `.env` files
- model weights, audio samples, build output, virtualenvs, caches

`scripts/check_repo_hygiene.sh` and the CI workflow enforce these rules. Test fixtures use synthetic data
only; never commit a real transcript or a personal photo.
