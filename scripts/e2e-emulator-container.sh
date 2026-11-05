#!/usr/bin/env bash
# Run the scripted-microphone E2E call test against the containerised relay.
#
# What it does, in order:
#   1. refuses to run without a booted emulator;
#   2. starts the relay stack with scripts/relay-container-up.sh only when
#      /ready does not already answer 200 (a healthy relay is never recreated);
#   3. forwards the relay port into the emulator with `adb reverse`;
#   4. issues a short-lived device token through the relay's admin CLI;
#   5. builds, installs, and runs exactly com.example.hermestalk.e2e.ContainerCallE2ETest;
#   6. prints the app-observed latency line from logcat and revokes the token.
#
# The test itself FAILS (it does not skip) when the container is absent, so a
# green run is a real call: real WebRTC, real relay, real STT/brain/TTS. Only the
# microphone samples are scripted; see docs/TESTING.md.
#
# Environment overrides: RELAY_PORT, ADB, COMPOSE, ANDROID_HOME, DEVICE_TOKEN_LABEL,
# TEST_CLASS.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DEPLOY="$ROOT/deploy"

RELAY_PORT=${RELAY_PORT:-8790}
ADB=${ADB:-adb}
COMPOSE=${COMPOSE:-docker compose}
DEVICE_TOKEN_LABEL=${DEVICE_TOKEN_LABEL:-e2e-emulator}
TEST_CLASS=${TEST_CLASS:-com.example.hermestalk.e2e.ContainerCallE2ETest}

if [ -z "${ANDROID_HOME:-}" ]; then
    if [ -d "${HOME}/Android/Sdk" ]; then
        export ANDROID_HOME="${HOME}/Android/Sdk"
    else
        printf 'e2e: set ANDROID_HOME to an Android SDK that carries platform-tools\n' >&2
        exit 2
    fi
fi
export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$ANDROID_HOME}"

if ! "$ADB" get-state >/dev/null 2>&1; then
    printf 'e2e: no emulator attached; start one with scripts/emulator-archive.sh headless\n' >&2
    exit 2
fi

ready() {
    local code
    code=$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${RELAY_PORT}/ready" || true)
    [ "$code" = "200" ]
}

if ready; then
    printf 'e2e: relay already healthy on 127.0.0.1:%s; leaving it alone\n' "$RELAY_PORT"
else
    printf 'e2e: relay is not answering /ready; starting the stack\n'
    RELAY_PORT="$RELAY_PORT" "$ROOT/scripts/relay-container-up.sh" --no-emulator
fi

brain_url=$(awk -F= '/^VOICE_BRAIN_BASE_URL=/{sub(/^[^=]*=/, ""); print; exit}' "$DEPLOY/.env" 2>/dev/null || true)
if [ -n "$brain_url" ]; then
    if ! curl -sf -m 5 "${brain_url}/v1/models" >/dev/null 2>&1; then
        printf 'e2e: warning — the voice-brain endpoint %s is not answering.\n' "$brain_url" >&2
        printf 'e2e: utterances that miss the deterministic policy route would speak the failure line.\n' >&2
    fi
fi

"$ADB" reverse "tcp:${RELAY_PORT}" "tcp:${RELAY_PORT}" >/dev/null
printf 'e2e: adb reverse tcp:%s tcp:%s in place\n' "$RELAY_PORT" "$RELAY_PORT"
# Clear logcat so the latency line printed below belongs to this run only.
"$ADB" logcat -c >/dev/null 2>&1 || true

# The admin CLI prints its audit log to stdout; the token is the one bare line.
token=$($COMPOSE -f "$DEPLOY/compose.yaml" exec -T relay \
    python -m relay.tokens issue --label "$DEVICE_TOKEN_LABEL" \
    | grep -v '^{' | grep -v '^issued ' | head -n1 | tr -d '\r')
if [ -z "$token" ]; then
    printf 'e2e: the relay did not return a device token\n' >&2
    exit 1
fi

cleanup() {
    "$COMPOSE" -f "$DEPLOY/compose.yaml" exec -T relay \
        python -m relay.tokens revoke --label "$DEVICE_TOKEN_LABEL" >/dev/null 2>&1 || true
    "$ADB" reverse --remove "tcp:${RELAY_PORT}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

printf 'e2e: issued a device token (label %s)\n' "$DEVICE_TOKEN_LABEL"

cd "$ROOT/android"
set +e
./gradlew :app:connectedDebugAndroidTest \
    -Pandroid.testInstrumentationRunnerArguments.class="$TEST_CLASS" \
    -Pandroid.testInstrumentationRunnerArguments.deviceToken="$token" \
    --console=plain
status=$?
set -e

printf '\n--- app-observed latency ---\n'
"$ADB" logcat -d -s ContainerCallE2E:I | tail -n 5 || true

printf '\n--- instrumentation result ---\n'
latest=$(find "$ROOT/android/app/build/outputs/androidTest-results/connected" -name '*.xml' -printf '%T@ %p\n' 2>/dev/null \
    | sort -n | tail -n1 | cut -d' ' -f2-)
if [ -n "$latest" ]; then
    printf '%s\n' "$latest"
    grep -h -o 'tests="[0-9]*" failures="[0-9]*" errors="[0-9]*" skipped="[0-9]*"' "$latest" || true
fi

if [ "$status" -ne 0 ]; then
    printf 'e2e: FAILED (gradle exit %s)\n' "$status" >&2
    exit "$status"
fi
printf 'e2e: PASSED\n'
