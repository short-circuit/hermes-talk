# Release process

This document is the gate for every tagged release: what must be true before a tag exists, how to verify
it, and what is recorded afterwards. It is also the record for v0.1.1 (see
[Verification record](#verification-record-v011)).

## Scope of v0.1.1

v0.1.1 is a pre-alpha baseline, not a finished product. It ships:

- the relay's probe surface (`/`, `/health`, `/ready`, `/v1/whoami`), the attachment inbox
  (`POST`/`PUT /v1/attachments`) and the artifact outbox (`GET`/`HEAD /v1/artifacts/{id}`);
- the Hermes run adapter, the delegation lifecycle, the spoken approval gate and the voice-brain router
  as libraries with offline test coverage;
- the Android client: RTVI transport, TLS pinning, wake word, push-to-talk, the call state machine and
  the attachments module, with unit and instrumentation tests;
- the deployment paths (Docker Compose and systemd), the operator runbook and the documentation set.

It does **not** ship the call loop that composes transport, speech, brain and Hermes into the relay app.
`relay.app` mounts no WebSocket/WebRTC route yet; the Android client talks to `android/stub-relay` for
signalling. Do not claim an end-to-end voice call as a v0.1.1 feature. The full gap list is under
[Known gaps](#known-gaps).

## Versioning

The version is declared in three places and they must agree:

| Source | Field |
|---|---|
| `relay/pyproject.toml` | `version` |
| `relay/__init__.py` | `__version__` (served by `/health` and `/`) |
| `android/app/build.gradle.kts` | `versionName` |

Android versioning is tied to the release tag:

- `versionName` follows the release tag exactly (`v<versionName>`); the literal `versionName` in
  `android/app/build.gradle.kts` is the Android source of truth.
- `versionCode` is **derived** in `android/app/build.gradle.kts` as
  `major * 10000 + minor * 100 + patch`. It can never drift from `versionName` and is monotonic for
  every SemVer increase. It is never edited by hand, never reused and never decreased. A version with
  `minor` or `patch` >= 100 is rejected by the build.
- v0.1.0's tagged debug APK used `versionCode = 1` and was never distributed. The derived scheme gives
  `100` for `0.1.0` and `200` for `0.2.0`, so no device can see a downgrade.
- Release builds are signed with a keystore that lives outside the repository. The signing credentials
  are environment variables or `~/.gradle/gradle.properties`, never committed.
- The release commit must not change the keystore or its credentials.

Rules:

- Releases use SemVer 2.0.0. Below 1.0.0 a minor bump may break compatibility; every breaking change is
  listed under **Changed** in `CHANGELOG.md` with a migration note.
- Tags are annotated and named `v<version>` (`v0.1.1`). A tag must never be moved or reused: if the
  contents change, cut a new patch version.
- A release commit updates the three sources, `CHANGELOG.md` and this document's verification record in
  one commit, so the tag points at a tree whose declared version matches its tag.

Consistency gate:

```bash
scripts/check_version.sh          # sources agree with each other
scripts/check_version.sh 0.1.1    # ... and with the intended release
```

The script exits non-zero on a missing or mismatched version. CI runs it in the `hygiene` job and, on a
tag ref, compares the sources with the tag name (`GITHUB_REF_NAME` without the `v`), so a mislabelled tag
cannot pass.

## Pre-release checklist

Run every step on a clean checkout of the exact commit that will be tagged. Nothing here restarts a
running service; the relay and the Hermes gateway are only started in throwaway processes or containers.

### 1. Repository state

```bash
git status --short          # must be empty
git log --oneline -1        # the release commit
```

### 2. Version and changelog

```bash
scripts/check_version.sh 0.1.1
```

`CHANGELOG.md` must have an entry for the version being tagged with **Added**, **Changed**, **Fixed**,
**Known limitations** and **Upgrade notes**.

### 3. Lint, format and tests (the `python` CI job)

From a clean clone, with a fresh virtualenv:

```bash
python3.12 -m venv venv
venv/bin/pip install -e "./relay[dev]"
mapfile -t files < <(git ls-files '*.py')
venv/bin/ruff check "${files[@]}"
venv/bin/ruff format --check "${files[@]}"
venv/bin/pytest relay/tests
```

Pass criteria: `All checks passed!`, `N files already formatted`, and a pytest run with zero failures
and zero errors. Speech tests skip cleanly when the optional model packages are absent.

### 4. Android (the `android` CI job)

```bash
cd android
ANDROID_HOME=$HOME/Android/Sdk ./gradlew test --rerun-tasks --no-daemon
ANDROID_HOME=$HOME/Android/Sdk ./gradlew assembleDebug --no-daemon
```

Pass criteria: `BUILD SUCCESSFUL` for both, zero test failures in the `TEST-*.xml` reports, and
`android/app/build/outputs/apk/debug/app-debug.apk` produced. CI builds debug only, so it needs no
signing secret.

Release signing is a local gate, run once with the four `HERMES_TALK_*` variables set (see
[Versioning](#versioning)):

```bash
HERMES_TALK_KEYSTORE_FILE=/absolute/path/to/hermes-talk-release.jks \
HERMES_TALK_KEYSTORE_PASSWORD=... \
HERMES_TALK_KEY_ALIAS=... \
HERMES_TALK_KEY_PASSWORD=... \
  ANDROID_HOME=$HOME/Android/Sdk ./gradlew assembleRelease --no-daemon
APKSIGNER=$(ls -d "$ANDROID_HOME"/build-tools/*/apksigner | sort -V | tail -1)
"$APKSIGNER" verify --print-certs app/build/outputs/apk/release/app-release.apk
```

Pass criteria: `BUILD SUCCESSFUL`, `android/app/build/outputs/apk/release/app-release.apk` produced, and
`apksigner verify --print-certs` reports the release certificate. Run `./gradlew assembleRelease` once
**without** the variables: it must fail before any task executes with the actionable "release signing is
not configured" message. That failure is part of the gate. The emulator instrumentation job needs a
running emulator; run it separately when one is available.

### 5. Repository hygiene and secrets (the `hygiene` and `secrets` CI jobs)

```bash
bash scripts/check_repo_hygiene.sh --self-test   # every rule must fire on its planted fixture
bash scripts/check_repo_hygiene.sh               # clean scan of the work tree
gitleaks git --log-opts=--all --redact           # full history, not just the tip
pre-commit run --all-files
```

Pass criteria: `hygiene self-test: PASS`, `hygiene: clean`, no gitleaks findings, and all pre-commit
hooks pass. The full-history scan is the one that catches a secret committed and later deleted.

### 6. Install from scratch

Follow [Install from scratch](#install-from-scratch) below in a throwaway directory. The relay must
answer its probes with the tagged version, and the Android build must produce a debug APK.

### 7. Packaging and licence

```bash
venv/bin/pip wheel ./relay --no-deps -w /tmp/wheels
python -m zipfile -l /tmp/wheels/hermes_talk_relay-0.1.1-py3-none-any.whl | grep -i license
cmp LICENSE relay/LICENSE
```

Pass criteria: the wheel contains `dist-info/licenses/LICENSE` and `METADATA` carries
`License-Expression: MIT` plus `License-File: LICENSE`; `cmp` exits 0. `relay/LICENSE` is a
byte-identical copy of the repository licence so the packaging pattern stays inside the project root —
setuptools rejects `..` in licence globs and deprecates patterns that use it. CI compares the two copies,
and the build must not emit a `Pattern ... cannot contain '..'` warning.

### 8. Deployment artefacts

```bash
cd deploy && cp ../.env.example .env && docker compose -f compose.yaml config --quiet
docker compose -f compose.yaml build relay
```

Pass criteria: `config --quiet` exits 0 and the relay image builds. `deploy/.env` is ignored by git.

### 9. Documentation

- `README.md` status line matches what actually ships.
- `docs/API.md` route inventory matches `create_app().openapi()` at the tag.
- `docs/ARCHITECTURE.md` wiring legend still says which components are mounted and which are libraries.
- `docs/PRIVACY.md` rule ids match `scripts/check_repo_hygiene.sh`.
- No documentation claims a feature the tag does not have.

### 10. Tag

Only after 1–9 pass:

```bash
git tag -a v0.1.1 -m "Hermes Talk v0.1.1"
git show --stat v0.1.1
```

Then re-run steps 2–6 **at the tag** in a fresh clone, because the tag is the artefact that matters.

## Install from scratch

These are the commands verified for v0.1.1, in the order a new contributor runs them.

### Relay, development install

```bash
git clone <repository> hermes-talk && cd hermes-talk
python3.12 -m venv venv
venv/bin/pip install -e "./relay[dev]"
export DATA_DIR=/var/lib/hermes-talk INBOX_DIR=/var/lib/hermes-talk/inbox \
       OUTBOX_DIR=/var/lib/hermes-talk/outbox RELAY_HOST=127.0.0.1 RELAY_PORT=8790 \
       HERMES_BASE_URL=http://127.0.0.1:8642 DEVICE_TOKENS=bootstrap-token-placeholder
venv/bin/python -m relay.app
```

`/health` answers with the version, `/v1/whoami` requires `Authorization: Bearer <token>`, and storage
directories must be outside the checkout. Python 3.11+ is supported; 3.12 is what CI and the container
use. `pip install -e "./relay[speech]"` adds the local STT/TTS engines.

### Relay container

```bash
cp .env.example deploy/.env      # fill in HERMES_API_KEY, RELAY_SITE_ADDRESS, HERMES_TALK_MODELS
docker compose -f deploy/compose.yaml up -d
```

The stack binds the relay to loopback and terminates TLS at Caddy with an internal CA. See
`docs/DEPLOYMENT.md` for host requirements and `docs/RUNBOOK.md` for start/stop, warm-up and token
commands.

### Android client

```bash
export ANDROID_HOME=$HOME/Android/Sdk      # or put sdk.dir in android/local.properties
cd android && ./gradlew assembleDebug
```

CI builds with JDK 17; the host used for the v0.1.0 check had JDK 21 and also built cleanly. Install the
APK with `adb install -r app/build/outputs/apk/debug/app-debug.apk` on a phone or emulator — never on the
living-room TV (see `CONTRIBUTING.md`).

## Licence inventory

The repository is MIT (`LICENSE`). Every runtime dependency is permissive; there is no GPL, LGPL or
AGPL code in the runtime path, and no dependency requires network access at runtime. Licences were read
from PyPI metadata, Google Maven and Maven Central POMs, and the vendored notices.

| Component | Licence | Source |
|---|---|---|
| Hermes Talk repository | MIT | `LICENSE` |
| fastapi, pydantic, pydantic-settings | MIT | PyPI metadata (`license_expression`) |
| uvicorn, httpx, pypdf, aiortc, av | BSD-3-Clause | PyPI metadata |
| python-multipart | Apache-2.0 | PyPI metadata |
| pillow | MIT-CMU | PyPI metadata |
| numpy | BSD-3-Clause (plus 0BSD, MIT, Zlib, CC0-1.0 components) | PyPI metadata |
| sherpa-onnx (Python and Android JNI) | Apache-2.0 | PyPI metadata, `android/wakeword/THIRD_PARTY.md` |
| onnxruntime (bundled with sherpa-onnx) | MIT | `android/wakeword/THIRD_PARTY.md` |
| kokoro-onnx | MIT | upstream `LICENSE` |
| faster-whisper | MIT | PyPI metadata |
| AndroidX (core, activity, lifecycle, security-crypto) and Material Components | Apache-2.0 | Google Maven POMs |
| kotlinx-coroutines, kotlinx-serialization | Apache-2.0 | Maven Central POMs |
| ai.pipecat:small-webrtc-transport | BSD-2-Clause (bundles libwebrtc and third-party notices) | Maven Central POM |

Models are not in the repository except the bundled keyword-spotting model, which is distributed with
sherpa-onnx under Apache-2.0 (`android/wakeword/THIRD_PARTY.md`). Operator-supplied models keep their own
licences: kokoro-v1.0 is Apache-2.0, Silero VAD is MIT, faster-whisper/Whisper models are MIT, and the
LFM voice-brain model is distributed under the LFM Open License — check each model card before
redistributing a bundle.

## Verification record (v0.1.1)

v0.1.1 is the published release. It supersedes v0.1.0: the v0.1.0 tree carries an invalid workflow file,
so GitHub Actions never ran for that tag and `CHANGELOG.md` marks it withdrawn. Apart from the version
strings and the workflow fix, the shipped code is the same, so the local reproduction below still
describes this tree.

The repository is published at <https://github.com/short-circuit/hermes-talk> and the workflow runs on
every push and pull request, so the tag ref is exercised by the provider rather than reproduced by hand.

CI evidence: the workflow is validated by the run on the release commit's `main` push and by the
`v0.1.1` tag run. Both run URLs and the per-job outcomes are recorded in the commit immediately after
the release commit, because a run URL only exists once the commit or tag has been pushed.

Local reproduction at the release tree (recorded on 2026-11-03 on Arch Linux x86_64, Python 3.12.16,
JDK 21, Docker with Compose v2):

| Check | Command | Result |
|---|---|---|
| Version consistency | `scripts/check_version.sh 0.1.1` | `check_version: ok (0.1.1)` |
| Python lint | `ruff check <tracked *.py>` | `All checks passed!` |
| Python format | `ruff format --check <tracked *.py>` | `86 files already formatted` |
| Relay tests | `pytest relay/tests` | `729 passed, 1 skipped` in 36.58s, exit 0 |
| Android unit tests | `./gradlew test --rerun-tasks` | `BUILD SUCCESSFUL`, 508 tests, 0 failures, 0 errors |
| Android build | `./gradlew assembleDebug` | `BUILD SUCCESSFUL`, `app-debug.apk` produced |
| Android instrumentation | `./gradlew :app:connectedDebugAndroidTest` with the CI arguments, on emulator-5554 (API 34) | 20 tests, 0 skipped, 0 failed, `BUILD SUCCESSFUL` in 52s |
| Hygiene self-test | `scripts/check_repo_hygiene.sh --self-test` | `PASS` (all seven rules fire, clean fixture passes) |
| Hygiene scan | `scripts/check_repo_hygiene.sh` | `hygiene: clean` (327 files scanned) |
| Full-history secrets | `gitleaks git --log-opts=--all --redact` | no findings (57 commits) |
| Allowlist proof | plant a `ghp_` token outside the allowlisted paths, `gitleaks dir .` | 2 findings on the planted file, none elsewhere |
| Pre-commit | `pre-commit run --all-files` | all hooks pass |
| Relay smoke (clean clone) | `python -m relay.app` + probes | `/health` → `{"status":"ok","service":"voice-relay","version":"0.1.1"}`; `/ready` → `ready`; `/v1/whoami` → 401 without a token, `{"label":"bootstrap"}` with the bootstrap token |
| Wheel licence | `pip wheel ./relay` | `dist-info/licenses/LICENSE` present; `License-Expression: MIT`, `License-File: LICENSE` |
| Compose config | `docker compose -f deploy/compose.yaml config --quiet` | exit 0 |
| Relay image | `docker compose -f deploy/compose.yaml build relay` | image `hermes-talk-relay:local` built |

Defects found and fixed on the way to the tag:

- the `python` CI job was red at `8171112`: 64 ruff findings and 5 unformatted files in `scripts/`,
  `spike/` and `android/stub-relay/`. Fixed without behaviour change; the kanban seed script's
  `--dry-run` output is byte-identical before and after.
- the wheel declared a licence expression but carried no licence file; `license-files` now ships it.
- `SECURITY.md` pointed at maintainer emails that did not exist; it now points at a private advisory.
- the first hosted run of the `android-instrumentation` job (`41557840345`) never reached the tests:
  `reactivecircus/android-emulator-runner` splits its `script` input into single lines and runs each in
  its own shell, so the `for` wait-loop and the `./gradlew` backslash continuation were passed to
  `/usr/bin/sh` one line at a time (`Syntax error: end of file unexpected (expecting "done")`) and the
  job then sat until its 45-minute timeout. The sequence now lives in `scripts/android-instrumentation.sh`
  and the step invokes it as a single line.

## Tagging and publication

1. Commit the release tree and tag it as described above.
2. Push the branch and the tag: `git push origin main && git push origin v0.1.1`.
3. Confirm the `secrets`, `hygiene`, `python`, `android` and `android-instrumentation` jobs are green
   **on the tag ref**, not just on the branch. The emulator job is the slowest; it must be green for a
   release.
4. If the relay image is published, tag it with the git short sha and record that sha in the release notes;
   `RELAY_IMAGE` in `deploy/.env` is the rollback handle.
5. Announce what the release does **not** do (see Scope) so users do not expect a working voice call.
6. Record the run URL and the per-job outcomes in this document in a follow-up commit — a run URL only
   exists after the push, so the release commit cannot carry it.

## Rollback

- Deployment rollback: pin `RELAY_IMAGE` to the previous short-sha tag and run
  `docker compose -f deploy/compose.yaml up -d` (or `systemctl restart hermes-talk`). The procedure is in
  `docs/DEPLOYMENT.md`; the relay's state (inbox, outbox, tokens, audit) is not touched by an image change.
- Release rollback: a tag is immutable. If the release is wrong, delete nothing — cut `v0.1.1` with the fix
  and mark the previous version as withdrawn in `CHANGELOG.md`.
- Never restart the Hermes gateway as part of a rollback without its owner's agreement; it is a separate
  service with in-flight sessions.

## Known gaps

- Call-loop wiring is not mounted (transport, speech, brain and Hermes are libraries; signalling comes
  from `android/stub-relay`).
- `POST /v1/runs` is reserved in the rate-limit table but not mounted.
- `MAX_CALL_MINUTES` is configured but not enforced.
- Release APKs must be built locally with signing credentials that live outside the repository. CI
  builds and tests debug only, so the first CI-verified signed APK is a local artefact.
- The GitHub Actions emulator job runs on a hosted `ubuntu-latest` runner with KVM enabled; the local
  reproduction used AVD `hermes_talk_34` on API 34. The hosted run is the production confirmation and is
  recorded in the verification record.
