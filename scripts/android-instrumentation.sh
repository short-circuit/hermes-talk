#!/usr/bin/env bash
# Runs the Android instrumentation suite against an already-booted CI emulator.
#
# `reactivecircus/android-emulator-runner` splits its `script` input into
# single lines and runs each one in a separate shell, so a `for` loop or a
# backslash continuation cannot live in the workflow step itself. This file
# keeps the whole sequence in one shell and is what the CI step invokes.
#
# Preconditions: an emulator is booted and `adb` is on PATH (the action sets
# both up), and `relay-venv` exists with the stub relay's dependencies
# installed (the workflow creates it in the preceding step).
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

RELAY_PORT=${RELAY_PORT:-8790}
PYTHON=${PYTHON:-relay-venv/bin/python}
HEALTH_URL="http://127.0.0.1:${RELAY_PORT}/health"

relay_pid=""
cleanup() {
    if [ -n "$relay_pid" ]; then
        kill "$relay_pid" 2>/dev/null || true
        wait "$relay_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT

"$PYTHON" android/stub-relay/relay.py --port "$RELAY_PORT" &
relay_pid=$!

for _ in $(seq 1 30); do
    if curl -sf "$HEALTH_URL" > /dev/null; then
        break
    fi
    sleep 1
done

if ! curl -sf "$HEALTH_URL" > /dev/null; then
    echo "instrumentation: stub relay did not answer ${HEALTH_URL}" >&2
    exit 1
fi

adb reverse "tcp:${RELAY_PORT}" "tcp:${RELAY_PORT}"

cd android
./gradlew :app:connectedDebugAndroidTest \
    -Pandroid.testInstrumentationRunnerArguments.notAnnotation=androidx.test.filters.LargeTest \
    -Pandroid.testInstrumentationRunnerArguments.screenOffSeconds=15 \
    --console=plain
