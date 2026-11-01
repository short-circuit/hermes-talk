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

The repository is public. Nothing personal may enter it — not in a commit, not in history, not in a
release artifact. Two gates enforce that: `scripts/check_repo_hygiene.sh` for content that is present in
the tree, and gitleaks for secrets anywhere in the commit history.

### Rules enforced by `scripts/check_repo_hygiene.sh`

Every rule below is implemented in that script and runs in CI (`.github/workflows/ci.yml`, job `hygiene`)
and in the pre-commit hook `repo-hygiene`. Rule ids are stable — cite them in review comments.

| Rule | Catches | Allowed exceptions |
|---|---|---|
| **R1** | personal email addresses | `*@example.com`, `*@example.org`, `*@example.net`, `*@users.noreply.github.com`, `noreply@*`, `git@github.com` |
| **R2** | absolute home-directory paths `/home/<user>/…` and `/Users/<user>/…` | `/home/runner/` (GitHub-hosted runner), documented placeholders such as `/home/<user>/…`, `$HOME`, `${DATA_DIR}` |
| **R3** | hard-coded RFC 1918 addresses — `10/8`, `172.16/12`, `192.168/16` | `127.0.0.1`, TEST-NET `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24` |
| **R4** | secret-shaped tokens: `sk-…`, `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`, `github_pat_…`, `xox[baprs]-…`, `AKIA…`, `AIza…`, `hf_…`, `glpat-…`, and `-----BEGIN … PRIVATE KEY-----` blocks | none — values belong in `.env` or CI secrets |
| **R5** | any file over the size cap (default 2 MiB) | none; raise the cap deliberately with `HYGIENE_MAX_BYTES` and say why in the PR |
| **R6** | committed environment files (`.env`, `.env.*`) | `.env.example` |
| **R7** | private-suffix hostnames (`*.local`, `*.lan`, `*.home`, `*.internal`) | synthetic `*.example.lan`, `*.example.local`, `*.example.home`, `*.example.internal` |

The script scans tracked files **and** untracked files that are not gitignored, so a leak is caught before
`git add`, not only after. It exits non-zero on any hit and prints `file:line:match` for each.

### Rules enforced by gitleaks

CI runs `gitleaks/gitleaks-action` with `fetch-depth: 0` on every push and every pull request, so the scan
covers the full history rather than the current tree. Pre-commit runs the gitleaks hook against the staged
index only; `pre-commit run --all-files` is therefore *not* a history scan. To check history locally:

```bash
gitleaks git --log-opts=--all --redact
```

### Proving the gate works

```bash
bash scripts/check_repo_hygiene.sh --self-test
```

The self-test plants one fixture per rule (a personal email, a `/home/…` path, a LAN address, a GitHub
token, an oversized file, a committed `.env`, an mDNS hostname), asserts each rule fires, asserts the
allowlisted forms stay silent, then builds a throwaway repository that must pass clean and must fail once
a secret is planted. CI runs it before the real scan, so a broken gate cannot report green.

When planting a fake secret by hand, use a high-entropy value: gitleaks drops findings that contain a
known placeholder stopword, and the alphabet run `abcdefghijklmnopqrstuvwxyz` is one. A `ghp_` token
built from that run is ignored by gitleaks while a random one fires; R4 flags both.

### Local use

```bash
pre-commit install                       # once per clone
pre-commit run --all-files               # hooks, staged-index gitleaks included
bash scripts/check_repo_hygiene.sh        # full work-tree PII scan
gitleaks git --log-opts=--all --redact   # full-history secret scan
```

### If a leak lands anyway

1. Treat the value as burned: rotate the credential before doing anything else. Removing the line does
   not un-publish it — the commit is already public.
2. Rewrite history with `git filter-repo` (or `git filter-branch` for small cases), force-push, and ask
   collaborators to re-clone. GitHub keeps unreachable objects reachable by SHA for a while; contact
   support only if the secret is high-value.
3. Add the finding to the PR description and note whether a rule or an allowlist entry needs adjusting.
4. Never "fix" a hit by adding an allowlist entry unless the value is genuinely synthetic.

Test fixtures use synthetic data only; never commit a real transcript, a personal photo, or a real
address.

## Retention

Lifetimes are configurable per store and disabled by default (`0` keeps data
until manual deletion).  The documented conservative values live in
`.env.example`; see `docs/DEPLOYMENT.md` § "Data retention and deletion" for the
windows, the pruner, and the scheduling examples.  Automated deletions are
written to the append-only audit trail with a relative path or session id, the
age, and the reason — never file contents or transcript text.
