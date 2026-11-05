#!/usr/bin/env bash
# Runs the Android instrumentation suite against an already-booted CI emulator.
#
# `reactivecircus/android-emulator-runner` splits its `script` input into
# single lines and runs each one in a separate shell, so a `for` loop or a
# backslash continuation cannot live in the workflow step itself. This file
# keeps the whole sequence in one shell and is what the CI step invokes.
#
# Phases:
#   1. stub relay on 127.0.0.1:8790 plus `adb reverse`, so the relay-backed call
#      tests have their in-process fixture;
#   2. install the APKs and grant the system assistant role through the shell
#      (`cmd role add-role-holder`), which a headless emulator cannot do through
#      the user dialog;
#   3. the default suite (non-@LargeTest) with the shortened screen-off window
#      and the shortened persistent-assistant soak, followed by a hard
#      verification that the assistant-role/keyevent-219, screen-off,
#      wake-word, per-fixture detection, call-handover and real-AudioRecord
#      cases actually ran and passed;
#   4. the @LargeTest wake-word screen-off soak with a shortened window, verified
#      the same way.
#
# The verifier fails on a skipped test and on a required case that is absent, so
# a filter that matches nothing or an annotation that silences a gate cannot
# leave the job green.
#
# Preconditions: an emulator is booted and `adb` is on PATH (the action sets
# both up), and `relay-venv` exists with the stub relay's dependencies
# installed (the workflow creates it in the preceding step).
#
# Environment: RELAY_PORT, PYTHON, ADB, SCREEN_OFF_SECONDS, WAKE_WORD_SOAK_SECONDS.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

RELAY_PORT=${RELAY_PORT:-8790}
PYTHON=${PYTHON:-relay-venv/bin/python}
ADB=${ADB:-adb}
HEALTH_URL="http://127.0.0.1:${RELAY_PORT}/health"
PACKAGE=com.example.hermestalk
ASSISTANT_ROLE=android.app.role.ASSISTANT
SCREEN_OFF_SECONDS=${SCREEN_OFF_SECONDS:-15}
WAKE_WORD_SOAK_SECONDS=${WAKE_WORD_SOAK_SECONDS:-120}
RESULTS_DIR="$ROOT/android/app/build/outputs/androidTest-results/connected"

relay_pid=""
cleanup() {
    if [ -n "$relay_pid" ]; then
        kill "$relay_pid" 2>/dev/null || true
        wait "$relay_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# The stub relay is the fixture the call tests expect: it accepts the empty
# bearer token a headless run passes. A real relay on the same port rejects it,
# so refuse to guess which one is answering.
if curl -sf "$HEALTH_URL" > /dev/null; then
    printf 'instrumentation: %s already answers; the stub relay cannot bind.\n' "$HEALTH_URL" >&2
    printf 'instrumentation: stop the other relay (docker compose -f deploy/compose.yaml stop relay) and rerun.\n' >&2
    exit 1
fi

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

"$ADB" reverse "tcp:${RELAY_PORT}" "tcp:${RELAY_PORT}"

grant_assistant_role() {
    # Install first: `cmd role add-role-holder` rejects a package that is not
    # installed. Re-granting is idempotent, and a reinstall by Gradle keeps the
    # role holder, so this is safe to repeat before every run.
    ( cd android && ./gradlew :app:installDebug :app:installDebugAndroidTest --console=plain )
    "$ADB" shell cmd role add-role-holder "$ASSISTANT_ROLE" "$PACKAGE"
    holders=$("$ADB" shell cmd role get-role-holders "$ASSISTANT_ROLE" | tr -d '\r')
    case "$holders" in
        *"$PACKAGE"*) printf 'instrumentation: assistant role held by %s\n' "$PACKAGE" ;;
        *)
            printf 'instrumentation: assistant role not held by %s (holders: %s)\n' \
                "$PACKAGE" "$holders" >&2
            exit 1
            ;;
    esac
}

grant_assistant_role

cd android
./gradlew :app:connectedDebugAndroidTest \
    -Pandroid.testInstrumentationRunnerArguments.notAnnotation=androidx.test.filters.LargeTest \
    -Pandroid.testInstrumentationRunnerArguments.screenOffSeconds="$SCREEN_OFF_SECONDS" \
    -Pandroid.testInstrumentationRunnerArguments.persistentSoakSeconds="$SCREEN_OFF_SECONDS" \
    --console=plain

cd "$ROOT"
bash scripts/verify-instrumentation-results.sh --dir "$RESULTS_DIR" \
    --require "com.example.hermestalk.assistant.AssistantRoleHeldTest#assistantRoleIsHeldByThisPackage" \
    --require "com.example.hermestalk.assistant.AssistantRoleHeldTest#assistKeyOpensTheAssistantOnTheCallScreen" \
    --require "com.example.hermestalk.call.CallForegroundServiceTest#callSurvivesScreenOffAndReleasesTheMicrophoneWhenEnded" \
    --require "com.example.hermestalk.persistent.PersistentAssistantServiceTest#screenOffAndDozeKeepTheServiceAlive" \
    --require "com.example.hermestalk.persistent.AssistantWakeWordServiceTest#wakeWordDetectionLaunchesTheAssistantWithinBudget" \
    --require "com.example.hermestalk.persistent.AssistantWakeWordServiceTest#fixturesAreDetectedWithZeroMissesAndNoFalseTriggers" \
    --require "com.example.hermestalk.persistent.AssistantWakeWordServiceTest#liveCallTakesTheMicrophoneAndReleasesTheAudioRecord" \
    --require "com.example.hermestalk.persistent.AssistantWakeWordServiceTest#realAudioRecordListenReleaseCycleWorks"

# The wake-word screen-off soak is @LargeTest: it is the one case the default
# suite excludes by annotation, so it gets its own explicit run here rather than
# being silently dropped. The window is shortened for CI; the default is 1800 s.
grant_assistant_role
cd android
./gradlew :app:connectedDebugAndroidTest \
    -Pandroid.testInstrumentationRunnerArguments.class=com.example.hermestalk.persistent.AssistantWakeWordServiceTest#continuousListeningForTheConfiguredWindow \
    -Pandroid.testInstrumentationRunnerArguments.wakeWordSoakSeconds="$WAKE_WORD_SOAK_SECONDS" \
    --console=plain

cd "$ROOT"
bash scripts/verify-instrumentation-results.sh --dir "$RESULTS_DIR" \
    --require "com.example.hermestalk.persistent.AssistantWakeWordServiceTest#continuousListeningForTheConfiguredWindow"
