#!/usr/bin/env bash
# fetch-e2e-models.sh — download the pinned, public model set the container E2E
# call needs, and verify every file against its SHA-256 before the relay mounts
# it.
#
# The weights are gitignored and never committed. Fetching them is not a cloud
# inference call: the relay still runs STT, the brain policy route and TTS on
# the local host/runner, and this script only populates the read-only model
# mount. Every download is checked against the digest recorded here, so a
# re-tagged upstream release cannot silently change what CI exercised.
#
# Usage:
#   MODELS_DIR=<dir> scripts/fetch-e2e-models.sh
#
# Environment: MODELS_DIR (required; deploy/.env HERMES_TALK_MODELS may supply
# it instead), CURL (default curl).
#
# Exit codes: 0 models present and verified, 1 download/verification failure,
# 2 usage error.
set -euo pipefail

CURL=${CURL:-curl}

MODELS_DIR=${MODELS_DIR:-${HERMES_TALK_MODELS:-}}
if [ -z "$MODELS_DIR" ]; then
    printf 'fetch-e2e-models: set MODELS_DIR (or HERMES_TALK_MODELS) to the model directory\n' >&2
    exit 2
fi
mkdir -p "$MODELS_DIR"

STT_TAR=sherpa-onnx-streaming-zipformer-en-2023-06-26.tar.bz2
STT_DIR=sherpa-onnx-streaming-zipformer-en-2023-06-26
STT_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/${STT_TAR}"
STT_SHA=639e25b578e9e997131402199419c13a941f8e4e198e2da1ce57dbf5cf401282

TTS_MODEL=kokoro-v1.0.int8.onnx
TTS_MODEL_URL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/${TTS_MODEL}"
TTS_MODEL_SHA=6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb

TTS_VOICES=voices-v1.0.bin
TTS_VOICES_URL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/${TTS_VOICES}"
TTS_VOICES_SHA=bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d

sha256_of() {
    sha256sum "$1" | cut -d' ' -f1
}

verified_file() {
    # verified_file <path> <sha256> <url>: download only when absent or stale.
    local path=$1 expected=$2 url=$3
    if [ -f "$path" ] && [ "$(sha256_of "$path")" = "$expected" ]; then
        printf 'fetch-e2e-models: %s already present and verified\n' "$path"
        return 0
    fi
    printf 'fetch-e2e-models: downloading %s\n' "$url"
    rm -f "${path}.part"
    "$CURL" -fsSL --retry 3 --retry-delay 2 -o "${path}.part" "$url"
    local actual
    actual=$(sha256_of "${path}.part")
    if [ "$actual" != "$expected" ]; then
        printf 'fetch-e2e-models: %s has sha256 %s, expected %s\n' "$url" "$actual" "$expected" >&2
        rm -f "${path}.part"
        return 1
    fi
    mv "${path}.part" "$path"
    printf 'fetch-e2e-models: verified %s\n' "$path"
}

verified_file "$MODELS_DIR/$TTS_MODEL" "$TTS_MODEL_SHA" "$TTS_MODEL_URL"
verified_file "$MODELS_DIR/$TTS_VOICES" "$TTS_VOICES_SHA" "$TTS_VOICES_URL"

# The sherpa archive is kept only long enough to extract it; the relay mounts
# the extracted directory, not the tarball. When the extraction already exists
# the archive is not downloaded again, and an operator's own copy is not
# deleted.
stt_tar="$MODELS_DIR/$STT_TAR"
if [ ! -f "$MODELS_DIR/$STT_DIR/encoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx" ]; then
    verified_file "$stt_tar" "$STT_SHA" "$STT_URL"
    printf 'fetch-e2e-models: extracting %s\n' "$stt_tar"
    tar -xjf "$stt_tar" -C "$MODELS_DIR"
    rm -f "$stt_tar"
else
    printf 'fetch-e2e-models: %s already extracted\n' "$STT_DIR"
fi

# The compose file points WHISPER_MODEL_DIR at this directory. The E2E run uses
# the sherpa engine, so the weights are not opened; the directory only has to
# exist for the mount layout to match docs/DEPLOYMENT.md.
mkdir -p "$MODELS_DIR/faster-whisper-base"

printf 'fetch-e2e-models: ready under %s\n' "$MODELS_DIR"
