# Contributing

## Ground rules

1. **No personal data in the repository.** No home-directory paths, personal email addresses, machine
   names, LAN IPs, or tokens. Use environment variables and example addresses from TEST-NET
   (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`).
2. **No secrets, ever.** `.env` is ignored; `.env.example` carries placeholders only. CI runs gitleaks on
   every push and pull request.
3. **No generated or vendored clutter.** Build output, virtualenvs, model weights, caches, and IDE state
   stay out of git (see `.gitignore`).
4. **Local-only.** A change that introduces a runtime dependency on an internet service needs an ADR.
5. **Never use the TV as a test device.** The living-room Chromecast is reachable over ADB but is not an
   Android test target: no `adb install`, no `am start`, nothing left running. Use an attached phone
   (`adb -s <serial>`) or an emulator; if neither exists, defer on-device checks.

## Workflow

- Branch from `main`; keep commits focused and imperative (`relay: add run event adapter`).
- Run `pre-commit install` once, then `pre-commit run --all-files` before pushing. The gitleaks hook
  scans staged content only; `bash scripts/check_repo_hygiene.sh` scans the whole work tree and
  `gitleaks git --log-opts=--all --redact` scans the full history.
- Run `scripts/check_repo_hygiene.sh` before opening a PR.
- Tests: `pytest relay/tests` and `./gradlew test` for the Android module.

## Review

Every PR needs a green CI run and one review. Security-relevant changes (auth, approvals, file handling)
also need a threat-model note in the PR description.
