#!/usr/bin/env bash
# Control the archive-hosted Android emulator.
#
# The AVD (`hermes_talk_34`, API 34, x86_64) lives on /store/archive so its
# 6 GiB data partition and snapshots stay off the small home disk; the SDK and
# the system image stay in the SDK root. Every subcommand exports the same
# ANDROID_AVD_HOME before touching avdmanager or emulator, so both tools agree
# on where the AVD is, and re-running a subcommand never creates a second AVD.
#
# Subcommands: start, headless, wait-for-boot, stop.
#
# Overridable: ANDROID_AVD_HOME, ANDROID_SDK_ROOT/ANDROID_HOME, AVD_NAME,
# SYSTEM_IMAGE, DEVICE_PROFILE, DATA_PARTITION_SIZE, BOOT_TIMEOUT.
set -euo pipefail

AVD_NAME=${AVD_NAME:-hermes_talk_34}
AVD_HOME=${ANDROID_AVD_HOME:-/store/archive/android-avd}
export ANDROID_AVD_HOME="$AVD_HOME"

SDK_ROOT=${ANDROID_SDK_ROOT:-${ANDROID_HOME:-$HOME/Android/Sdk}}
export ANDROID_SDK_ROOT="$SDK_ROOT"
export PATH="$SDK_ROOT/platform-tools:$SDK_ROOT/emulator:$SDK_ROOT/cmdline-tools/latest/bin:$PATH"

SYSTEM_IMAGE=${SYSTEM_IMAGE:-system-images;android-34;google_apis;x86_64}
DEVICE_PROFILE=${DEVICE_PROFILE:-pixel_6}
DATA_PARTITION_SIZE=${DATA_PARTITION_SIZE:-6442450944}
BOOT_TIMEOUT=${BOOT_TIMEOUT:-600}
# Quickboot snapshots live next to the AVD on /store/archive and make restarts
# fast; COLD_BOOT=yes skips loading one so a boot is reproducible from scratch.
COLD_BOOT=${COLD_BOOT:-no}
LOG_FILE="$AVD_HOME/emulator-$AVD_NAME.log"

log() { printf '[emulator-archive] %s\n' "$*" >&2; }

usage() {
    cat >&2 <<EOF
usage: $(basename "$0") <command>

commands:
  start          create the AVD if missing, then launch it with a window
  headless       create the AVD if missing, then launch it without a window
  wait-for-boot  wait until the emulator reports sys.boot_completed=1
  stop           shut the running emulator down

environment:
  ANDROID_AVD_HOME=$AVD_HOME
  ANDROID_SDK_ROOT=$SDK_ROOT
  AVD_NAME=$AVD_NAME  SYSTEM_IMAGE=$SYSTEM_IMAGE  BOOT_TIMEOUT=$BOOT_TIMEOUT
  COLD_BOOT=$COLD_BOOT (yes = skip the quickboot snapshot, forces a real cold boot)
EOF
}

avd_exists() { [ -f "$AVD_HOME/$AVD_NAME.ini" ] && [ -d "$AVD_HOME/$AVD_NAME.avd" ]; }

# Serials of emulators that adb currently sees (the device may be `offline`
# while it boots; only the serial matters here).
running_serials() {
    adb devices 2>/dev/null | awk '/^emulator-[0-9]+[[:space:]]/ {print $1}' || true
}

ensure_avd() {
    if avd_exists; then
        log "reusing existing AVD $AVD_NAME at $AVD_HOME/$AVD_NAME.avd"
        return 0
    fi
    log "creating AVD $AVD_NAME from $SYSTEM_IMAGE (device profile $DEVICE_PROFILE)"
    mkdir -p "$AVD_HOME"
    echo no | avdmanager create avd \
        --name "$AVD_NAME" \
        --package "$SYSTEM_IMAGE" \
        --device "$DEVICE_PROFILE" \
        --force
    # avdmanager hands out an 800M data partition by default; the app's install
    # and upgrade flows need room, so size it like the original 6 GiB AVD.
    local config="$AVD_HOME/$AVD_NAME.avd/config.ini"
    sed -i "s/^disk\.dataPartition\.size=.*/disk.dataPartition.size=$DATA_PARTITION_SIZE/" "$config"
    log "AVD ready: $(du -sh "$AVD_HOME/$AVD_NAME.avd" | cut -f1) on disk"
}

launch() {
    local mode=$1 headless=$2
    ensure_avd
    local serials
    serials=$(running_serials)
    if [ -n "$serials" ]; then
        log "emulator already running ($serials); not starting another"
        return 0
    fi
    local args=(-avd "$AVD_NAME" -no-boot-anim -netdelay none -netspeed full)
    if [ "$headless" = yes ]; then
        args+=(-no-window -no-audio -gpu swiftshader_indirect)
    fi
    if [ "$COLD_BOOT" = yes ]; then
        args+=(-no-snapshot-load)
        log "COLD_BOOT=yes: ignoring any saved snapshot"
    fi
    log "starting emulator $AVD_NAME ($mode), output: $LOG_FILE"
    nohup emulator "${args[@]}" >"$LOG_FILE" 2>&1 &
    log "emulator pid $! — run '$(basename "$0") wait-for-boot' next"
}

wait_for_boot() {
    local start=$SECONDS serial=""
    while [ $((SECONDS - start)) -lt "$BOOT_TIMEOUT" ]; do
        serial=$(running_serials | head -n1)
        [ -n "$serial" ] && break
        sleep 2
    done
    if [ -z "$serial" ]; then
        log "no emulator serial appeared within ${BOOT_TIMEOUT}s"
        return 1
    fi
    log "waiting for $serial to finish booting"
    while [ $((SECONDS - start)) -lt "$BOOT_TIMEOUT" ]; do
        if [ "$(adb -s "$serial" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; then
            log "boot completed on $serial after $((SECONDS - start))s"
            log "sdk=$(adb -s "$serial" shell getprop ro.build.version.sdk | tr -d '\r') abi=$(adb -s "$serial" shell getprop ro.product.cpu.abi | tr -d '\r')"
            return 0
        fi
        sleep 2
    done
    log "timed out after ${BOOT_TIMEOUT}s waiting for $serial"
    return 1
}

stop_emulator() {
    local serials
    serials=$(running_serials)
    if [ -z "$serials" ]; then
        log "no running emulator"
        return 0
    fi
    local s
    for s in $serials; do
        log "shutting down $s"
        adb -s "$s" emu kill >/dev/null 2>&1 || true
    done
    local i
    for i in $(seq 1 60); do
        if [ -z "$(running_serials)" ]; then
            log "emulator stopped"
            return 0
        fi
        sleep 1
    done
    log "emulator still listed after 60s; sending SIGTERM to its process"
    pkill -f "emulator.*-avd $AVD_NAME" 2>/dev/null || true
}

case "${1:-}" in
    start) launch start no ;;
    headless) launch headless yes ;;
    wait-for-boot) wait_for_boot ;;
    stop) stop_emulator ;;
    -h|--help|help|"") usage; [ -z "${1:-}" ] && exit 2 || exit 0 ;;
    *) usage; exit 2 ;;
esac
