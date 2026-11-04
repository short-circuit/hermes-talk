#!/usr/bin/env bash
# Tear the Hermes Talk container stack down and drop the emulator forward.
#
# Keeps the relay-data volume by default, because it holds the device token
# state; pass --volumes to delete it as well. Only containers labelled with the
# hermes-talk compose project are touched.
#
# Options:
#   --volumes  remove the named volumes too (device tokens, Caddy data)
#   --help     show this text
#
# Environment overrides: RELAY_PORT, ADB, COMPOSE.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DEPLOY="$ROOT/deploy"

RELAY_PORT=${RELAY_PORT:-8790}
ADB=${ADB:-adb}
COMPOSE=${COMPOSE:-docker compose}

remove_volumes=0
for arg in "$@"; do
    case "$arg" in
        --volumes) remove_volumes=1 ;;
        --help|-h)
            sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            printf 'relay-container-down: unknown option %s (see --help)\n' "$arg" >&2
            exit 2
            ;;
    esac
done

cd "$DEPLOY"

down_args=(down --remove-orphans)
if [ "$remove_volumes" -eq 1 ]; then
    down_args+=(--volumes)
fi
$COMPOSE "${down_args[@]}"

if "$ADB" get-state >/dev/null 2>&1; then
    "$ADB" reverse --remove "tcp:${RELAY_PORT}" >/dev/null 2>&1 || true
    printf 'relay-container-down: removed adb reverse tcp:%s\n' "$RELAY_PORT"
fi

leftover=$(docker ps -aq --filter "label=com.docker.compose.project=hermes-talk")
if [ -n "$leftover" ]; then
    printf 'relay-container-down: containers still present:\n' >&2
    docker ps -a --filter "label=com.docker.compose.project=hermes-talk" >&2
    exit 1
fi
printf 'relay-container-down: no hermes-talk container left; volumes %s\n' \
    "$([ "$remove_volumes" -eq 1 ] && echo removed || echo kept)"
