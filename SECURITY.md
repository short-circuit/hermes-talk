# Security

## Reporting

Open a private security advisory on GitHub, or email the maintainers listed in `pyproject.toml`.
Please do not open a public issue for vulnerabilities.

## Model

See `docs/security/threat-model.md` for the STRIDE register and the card that owns each mitigation.

- The relay and the Hermes API server bind to loopback or the LAN; nothing is exposed to the internet.
- Devices authenticate with per-device bearer tokens over TLS.
- Destructive tool calls require a spoken confirmation; silence means deny.
- Attachments are validated (MIME sniff, size cap), stored outside the repo, and never executed.
- Every turn is logged locally: transcript, tool calls, approvals, attachments, latency.

## Repository hygiene

CI enforces:
- gitleaks (secret scanning)
- a PII grep for emails, home paths, and hard-coded LAN addresses
- a 2 MB file-size cap

See `docs/PRIVACY.md` for the full data-flow statement.
