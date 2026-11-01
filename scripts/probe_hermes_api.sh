#!/usr/bin/env bash
# Probe the local Hermes API server and print a copy-pasteable transcript.
#
#   HERMES_API_KEY=... ./scripts/probe_hermes_api.sh capabilities runs sse stop \
#       sessions uploads ownership sessionkey approval clarify
#
# Every section prints the curl command it runs (with the bearer token kept in
# $HERMES_API_KEY so the transcript stays secret-free) followed by the raw
# response body and HTTP status. Nothing here writes to the repository.
#
# Environment:
#   HERMES_API_KEY       required, bearer token for the API server
#   HERMES_BASE_URL      optional, default http://127.0.0.1:8642
#   FOREIGN_SESSION_ID   optional, a session owned by another surface (ownership section)
set -uo pipefail

BASE="${HERMES_BASE_URL:-http://127.0.0.1:8642}"
KEY="${HERMES_API_KEY:-}"
if [ -z "$KEY" ]; then
  echo "HERMES_API_KEY is not set" >&2
  exit 1
fi

AUTH_DISPLAY='-H "Authorization: Bearer $HERMES_API_KEY"'
JSON_DISPLAY='-H "Content-Type: application/json"'

show() { # label, display-command, curl args...
  local label="$1" display="$2"
  shift 2
  printf '\n--- %s ---\n$ %s\n' "$label" "$display"
  curl -sS -w '\n[HTTP %{http_code}]\n' "$@"
}

auth=(-H "Authorization: Bearer $KEY")
json=(-H "Content-Type: application/json")

sec_capabilities() {
  show "health, unauthenticated" "curl -sS $BASE/health" "$BASE/health"
  show "capabilities, no bearer token" \
    "curl -sS $BASE/v1/capabilities" "$BASE/v1/capabilities"
  show "capabilities, authenticated" \
    "curl -sS $AUTH_DISPLAY $BASE/v1/capabilities" "${auth[@]}" "$BASE/v1/capabilities"
  show "models, authenticated" \
    "curl -sS $AUTH_DISPLAY $BASE/v1/models" "${auth[@]}" "$BASE/v1/models"
}

sec_runs() {
  show "create run" \
    "curl -sS $AUTH_DISPLAY $JSON_DISPLAY -X POST $BASE/v1/runs -d '{\"input\":\"Reply with the single word: pong\"}'" \
    "${auth[@]}" "${json[@]}" -X POST "$BASE/v1/runs" \
    -d '{"input":"Reply with the single word: pong"}'
}

sec_sse() { # run id
  local rid="${1:?run id}"
  show "event stream (SSE)" \
    "curl -sS -N $AUTH_DISPLAY -H \"Accept: text/event-stream\" $BASE/v1/runs/$rid/events" \
    "${auth[@]}" -H "Accept: text/event-stream" -N --max-time 90 "$BASE/v1/runs/$rid/events"
}

sec_status() { # run id
  local rid="${1:?run id}"
  show "run status" "curl -sS $AUTH_DISPLAY $BASE/v1/runs/$rid" "${auth[@]}" "$BASE/v1/runs/$rid"
}

sec_stop() { # run id
  local rid="${1:?run id}"
  show "stop run" \
    "curl -sS $AUTH_DISPLAY $JSON_DISPLAY -X POST $BASE/v1/runs/$rid/stop -d '{}'" \
    "${auth[@]}" "${json[@]}" -X POST "$BASE/v1/runs/$rid/stop" -d '{}'
}

sec_sessions() {
  show "list sessions" \
    "curl -sS $AUTH_DISPLAY '$BASE/api/sessions?limit=3'" "${auth[@]}" "$BASE/api/sessions?limit=3"
  local sid
  sid=$(curl -sS "${auth[@]}" "${json[@]}" -X POST "$BASE/api/sessions" -d '{"title":"probe"}' \
        | jq -r '.session_id // .id // empty')
  printf '\n(created session id: %s)\n' "${sid:-<none>}"
  if [ -n "$sid" ]; then
    show "session chat" \
      "curl -sS $AUTH_DISPLAY $JSON_DISPLAY -X POST $BASE/api/sessions/$sid/chat -d '{\"input\":\"Reply with the single word: pong\"}'" \
      "${auth[@]}" "${json[@]}" -X POST "$BASE/api/sessions/$sid/chat" \
      -d '{"input":"Reply with the single word: pong"}'
    show "session messages" \
      "curl -sS $AUTH_DISPLAY '$BASE/api/sessions/$sid/messages'" \
      "${auth[@]}" "$BASE/api/sessions/$sid/messages"
    show "delete session" \
      "curl -sS $AUTH_DISPLAY -X DELETE $BASE/api/sessions/$sid" \
      "${auth[@]}" -X DELETE "$BASE/api/sessions/$sid"
  fi
}

sec_uploads() {
  show "uploaded file part rejected (chat/completions)" \
    "curl -sS $AUTH_DISPLAY $JSON_DISPLAY -X POST $BASE/v1/chat/completions -d '{\"model\":\"hermes-agent\",\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"hi\"},{\"type\":\"file\",\"file\":{\"filename\":\"notes.txt\",\"file_data\":\"aGk=\"}}]}]}'" \
    "${auth[@]}" "${json[@]}" -X POST "$BASE/v1/chat/completions" \
    -d '{"model":"hermes-agent","messages":[{"role":"user","content":[{"type":"text","text":"hi"},{"type":"file","file":{"filename":"notes.txt","file_data":"aGk="}}]}]}'
  show "input_file part rejected (responses)" \
    "curl -sS $AUTH_DISPLAY $JSON_DISPLAY -X POST $BASE/v1/responses -d '{\"model\":\"hermes-agent\",\"input\":[{\"type\":\"input_text\",\"text\":\"hi\"},{\"type\":\"input_file\",\"filename\":\"notes.txt\",\"file_data\":\"aGk=\"}]}'" \
    "${auth[@]}" "${json[@]}" -X POST "$BASE/v1/responses" \
    -d '{"model":"hermes-agent","input":[{"type":"input_text","text":"hi"},{"type":"input_file","filename":"notes.txt","file_data":"aGk="}]}'
  show "non-image data URL rejected" \
    "curl -sS $AUTH_DISPLAY $JSON_DISPLAY -X POST $BASE/v1/chat/completions -d '{\"model\":\"hermes-agent\",\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"hi\"},{\"type\":\"image_url\",\"image_url\":{\"url\":\"data:text/plain;base64,aGk=\"}}]}]}'" \
    "${auth[@]}" "${json[@]}" -X POST "$BASE/v1/chat/completions" \
    -d '{"model":"hermes-agent","messages":[{"role":"user","content":[{"type":"text","text":"hi"},{"type":"image_url","image_url":{"url":"data:text/plain;base64,aGk="}}]}]}'
  show "inline image accepted" \
    "curl -sS $AUTH_DISPLAY $JSON_DISPLAY -X POST $BASE/v1/chat/completions -d '{\"model\":\"hermes-agent\",\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"What colour is this 1x1 image? Answer with one word.\"},{\"type\":\"image_url\",\"image_url\":{\"url\":\"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==\"}}]}]}'" \
    "${auth[@]}" "${json[@]}" -X POST "$BASE/v1/chat/completions" \
    -d '{"model":"hermes-agent","messages":[{"role":"user","content":[{"type":"text","text":"What colour is this 1x1 image? Answer with one word."},{"type":"image_url","image_url":{"url":"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="}}]}]}'
}

sec_ownership() {
  local foreign="${FOREIGN_SESSION_ID:-}"
  if [ -z "$foreign" ]; then
    printf '\n(ownership section skipped: set FOREIGN_SESSION_ID)\n'
    return
  fi
  show "run bound to a session owned by another surface" \
    "curl -sS $AUTH_DISPLAY $JSON_DISPLAY -X POST $BASE/v1/runs -d '{\"input\":\"hi\",\"session_id\":\"$foreign\"}'" \
    "${auth[@]}" "${json[@]}" -X POST "$BASE/v1/runs" \
    -d "{\"input\":\"hi\",\"session_id\":\"$foreign\"}"
  show "same foreign session on chat/completions" \
    "curl -sS $AUTH_DISPLAY $JSON_DISPLAY -H \"X-Hermes-Session-Id: $foreign\" -X POST $BASE/v1/chat/completions -d '{\"model\":\"hermes-agent\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'" \
    "${auth[@]}" "${json[@]}" -H "X-Hermes-Session-Id: $foreign" \
    -X POST "$BASE/v1/chat/completions" \
    -d '{"model":"hermes-agent","messages":[{"role":"user","content":"hi"}]}'
}

sec_sessionkey() {
  local long
  long=$(printf 'k%.0s' $(seq 1 257))
  show "session key echoed on a run" \
    "curl -sS $AUTH_DISPLAY $JSON_DISPLAY -H 'X-Hermes-Session-Key: probe:channel:1' -X POST $BASE/v1/runs -d '{\"input\":\"Reply with the single word: pong\"}'" \
    "${auth[@]}" "${json[@]}" -H 'X-Hermes-Session-Key: probe:channel:1' \
    -X POST "$BASE/v1/runs" -d '{"input":"Reply with the single word: pong"}'
  show "session key longer than 256 chars" \
    "curl -sS $AUTH_DISPLAY $JSON_DISPLAY -H \"X-Hermes-Session-Key: $long\" -X POST $BASE/v1/runs -d '{\"input\":\"hi\"}'" \
    "${auth[@]}" "${json[@]}" -H "X-Hermes-Session-Key: $long" \
    -X POST "$BASE/v1/runs" -d '{"input":"hi"}'
}

for section in "${@:-all}"; do
  case "$section" in
    capabilities) sec_capabilities ;;
    runs) sec_runs ;;
    sessions) sec_sessions ;;
    uploads) sec_uploads ;;
    ownership) sec_ownership ;;
    sessionkey) sec_sessionkey ;;
    all)
      sec_capabilities; sec_runs; sec_sessions; sec_uploads; sec_ownership; sec_sessionkey ;;
    *) printf 'unknown section: %s\n' "$section" >&2 ;;
  esac
done
