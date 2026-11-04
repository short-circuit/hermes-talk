#!/usr/bin/env bash
# Bring the Hermes Talk relay up as the committed container stack and wire the
# Android emulator to it.
#
# What it does, in order:
#   1. prepares deploy/.env from .env.example when it is missing;
#   2. builds and starts the relay (and volume-init) from deploy/compose.yaml;
#   3. waits for the container healthcheck to report healthy;
#   4. forwards the relay port into a booted emulator with `adb reverse`.
#
# The relay binds the host loopback only, so `adb reverse` is the supported
# emulator path. The emulator's built-in host alias reaches the host loopback
# too, but it is not used here: a forwarded port is what a phone uses through
# the proxy, and it keeps the tests independent of emulator networking.
#
# Options:
#   --with-proxy   also start the Caddy proxy service
#   --no-emulator  skip the `adb reverse` step
#   --help         show this text
#
# Environment overrides: RELAY_PORT, HEALTH_TIMEOUT, ADB, COMPOSE.
# Nothing outside the hermes-talk compose project is started or restarted.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DEPLOY="$ROOT/deploy"

RELAY_PORT=${RELAY_PORT:-8790}
HEALTH_TIMEOUT=${HEALTH_TIMEOUT:-180}
ADB=${ADB:-adb}
COMPOSE=${COMPOSE:-docker compose}

with_proxy=0
with_emulator=1
for arg in "$@"; do
    case "$arg" in
        --with-proxy) with_proxy=1 ;;
        --no-emulator) with_emulator=0 ;;
        --help|-h)
            sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            printf 'relay-container-up: unknown option %s (see --help)\n' "$arg" >&2
            exit 2
            ;;
    esac
done

cd "$DEPLOY"

if [ ! -f .env ]; then
    cp ../.env.example .env
    chmod 600 .env
    printf 'relay-container-up: created deploy/.env from .env.example; review it before deploying.\n'
fi

# .env is not sourced: values such as APPROVAL_CONFIRMATION_PHRASE contain
# unquoted spaces, so the file is valid for compose but not for `sh`. The two
# values this script needs are read individually instead.
env_model_dir=$(awk -F= '/^HERMES_TALK_MODELS=/{sub(/^[^=]*=/, ""); print; exit}' .env)
models_dir=${HERMES_TALK_MODELS:-$env_model_dir}
models_dir=${models_dir%\"}
models_dir=${models_dir#\"}
if [ -z "$models_dir" ]; then
    printf 'relay-container-up: HERMES_TALK_MODELS is not set in deploy/.env\n' >&2
    exit 2
fi
if [ ! -d "$models_dir" ]; then
    printf 'relay-container-up: model directory %s does not exist\n' "$models_dir" >&2
    exit 2
fi

services=(volume-init relay)
if [ "$with_proxy" -eq 1 ]; then
    services+=(proxy)
fi

# Reports healthy once the container's own healthcheck passes; fails fast on a
# terminal state instead of burning the whole timeout.
wait_healthy() {
    local service=$1 timeout=$2 deadline id status
    deadline=$((SECONDS + timeout))
    while [ "$SECONDS" -lt "$deadline" ]; do
        id=$($COMPOSE ps -q "$service" 2>/dev/null | head -n1)
        if [ -n "$id" ]; then
            status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null || echo unknown)
            case "$status" in
                healthy) printf 'healthy\n'; return 0 ;;
                unhealthy|exited|dead) printf '%s\n' "$status"; return 1 ;;
            esac
        fi
        sleep 3
    done
    printf 'timeout\n'
    return 1
}

printf 'relay-container-up: building and starting %s\n' "${services[*]}"
$COMPOSE up -d --build "${services[@]}"

printf 'relay-container-up: waiting for the relay healthcheck (timeout %ss)\n' "$HEALTH_TIMEOUT"
if ! health=$(wait_healthy relay "$HEALTH_TIMEOUT"); then
    printf 'relay-container-up: relay is %s after %ss; last checks:\n' "$health" "$HEALTH_TIMEOUT" >&2
    $COMPOSE ps >&2 || true
    printf 'relay-container-up: docker compose logs --tail 50 relay\n' >&2
    exit 1
fi

# The healthcheck already probes /health and /ready inside the container; repeat
# it from the host so a broken host-side path is not mistaken for a healthy one.
for path in /health /ready; do
    code=$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${RELAY_PORT}${path}" || true)
    if [ "$code" != "200" ]; then
        printf 'relay-container-up: http://127.0.0.1:%s%s returned %s\n' "$RELAY_PORT" "$path" "${code:-no answer}" >&2
        exit 1
    fi
done
printf 'relay-container-up: relay healthy; /health and /ready answer 200 on 127.0.0.1:%s\n' "$RELAY_PORT"

if [ "$with_proxy" -eq 1 ]; then
    if ! proxy_health=$(wait_healthy proxy 60); then
        printf 'relay-container-up: proxy is %s; docker compose logs --tail 20 proxy\n' "$proxy_health" >&2
        printf 'relay-container-up: a listener on 443 (for example tailscaled) makes Caddy fail to bind; set RELAY_SITE_ADDRESS to a host:port that is free\n' >&2
        exit 1
    fi
    env_site_address=$(awk -F= '/^RELAY_SITE_ADDRESS=/{sub(/^[^=]*=/, ""); print; exit}' .env)
    printf 'relay-container-up: proxy healthy; TLS entry point %s\n' "${RELAY_SITE_ADDRESS:-${env_site_address:-voice.example.lan}}"
fi

if [ "$with_emulator" -eq 1 ]; then
    if "$ADB" get-state >/dev/null 2>&1; then
        "$ADB" reverse "tcp:${RELAY_PORT}" "tcp:${RELAY_PORT}" >/dev/null
        printf 'relay-container-up: adb reverse tcp:%s tcp:%s is in place; the emulator reaches http://127.0.0.1:%s\n' \
            "$RELAY_PORT" "$RELAY_PORT" "$RELAY_PORT"
    else
        printf 'relay-container-up: no emulator attached; skipped adb reverse (start one with scripts/emulator-archive.sh headless)\n'
    fi
fi

if [ "$with_proxy" -eq 0 ]; then
    printf 'relay-container-up: proxy not started; add --with-proxy when 443 and 80 are free\n'
fi
