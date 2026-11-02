#!/usr/bin/env bash
# Probe the Hermes API-server contract end to end and print a transcript.
#
# Every section prints the curl command it ran (with the bearer token shown as
# $HERMES_API_KEY) followed by the verbatim response body and HTTP status, so the
# output can be pasted into docs/research/hermes-api.md.
#
# Prerequisites:
#   * the gateway is running with platforms.api_server.enabled: true and
#     API_SERVER_KEY set (>=16 chars);
#   * a LOCAL model provider (this probe never calls a cloud API);
#   * nothing else needs the gateway's approval channel while it runs.
#
# The script temporarily edits two settings in the live Hermes config and restores
# them on exit (trap):
#   approvals.mode = manual         smart mode may auto-approve a dangerous
#                                   command, so no approval prompt would appear
#   platform_toolsets.api_server   += clarify: clarify is off for api_server by
#                                   default, so no clarify prompt would appear
# Set HERMES_PROBE_NO_CONFIG=1 to skip both edits (then the approval and clarify
# sections are skipped too).
#
# Usage:
#   HERMES_API_KEY=... scripts/probe_hermes_api.sh 2>&1 | tee /tmp/hermes-api.txt
set -uo pipefail

BASE=${HERMES_API_BASE:-http://127.0.0.1:8642}
ENV_FILE=${HERMES_ENV_FILE:-$HOME/.hermes/.env}
CONFIG_FILE=${HERMES_CONFIG:-$HOME/.hermes/config.yaml}
BACKUP="${CONFIG_FILE}.probe-backup"
NO_CONFIG=${HERMES_PROBE_NO_CONFIG:-0}
SSE_WAIT=${HERMES_PROBE_SSE_WAIT:-120}
IDEM_KEY="probe-$(date +%s)-$$"   # unique per run: the idempotency store is durable for 24h
PROBE_TMP=$(mktemp -d)             # scratch dir for response headers; removed on exit

KEY=${HERMES_API_KEY:-}
if [ -z "$KEY" ] && [ -f "$ENV_FILE" ]; then
  KEY=$(python3 -c "import re,sys;m=re.search(r'^API_SERVER_KEY=(.*)\$',open(sys.argv[1]).read(),re.M);print(m.group(1).strip() if m else '')" "$ENV_FILE")
fi
if [ -z "$KEY" ]; then
  echo "no API key: set HERMES_API_KEY or put API_SERVER_KEY in $ENV_FILE" >&2
  exit 1
fi

# ---------------------------------------------------------------- config knobs
# Both edits are line-based so the rest of the file (comments, order) survives.
# They must leave a config the gateway can parse: a duplicate `api_server:` key
# under platform_toolsets makes every agent turn fail with
# "terminal policy unavailable ... cannot parse config.yaml".
cfg_mode() {
  python3 - "$CONFIG_FILE" <<'PY'
import sys
lines = open(sys.argv[1]).read().splitlines()
i = lines.index("approvals:")
for ln in lines[i + 1:]:
    if ln.startswith("  mode:"):
        print(ln.split(":", 1)[1].strip())
        break
PY
}
cfg_set_mode() {
  python3 - "$CONFIG_FILE" "$1" <<'PY'
import sys
p, val = sys.argv[1], sys.argv[2]
lines = open(p).read().splitlines()
i = lines.index("approvals:")
for n in range(i + 1, len(lines)):
    if lines[n].startswith("  mode:"):
        lines[n] = "  mode: " + val
        break
open(p, "w").write("\n".join(lines) + "\n")
PY
}
cfg_toolsets_has_clarify() {
  python3 - "$CONFIG_FILE" <<'PY'
import sys
lines = open(sys.argv[1]).read().splitlines()
i = lines.index("platform_toolsets:")
block, seen = [], 0
for ln in lines[i + 1:]:
    if ln and not ln.startswith(" "):
        break
    if ln.startswith("  api_server:"):
        seen += 1
    block.append(ln)
if seen > 1:
    sys.exit("duplicate api_server key under platform_toolsets")
sys.exit(0 if any("clarify" in ln for ln in block) else 1)
PY
}
cfg_toolsets_add_clarify() {
  python3 - "$CONFIG_FILE" <<'PY'
import sys
p = sys.argv[1]
lines = open(p).read().splitlines()
i = lines.index("platform_toolsets:")
end = len(lines)
for n in range(i + 1, len(lines)):
    if lines[n] and not lines[n].startswith(" "):
        end = n
        break
block = lines[i + 1:end]
hits = [n for n, ln in enumerate(block) if ln.startswith("  api_server:")]
if len(hits) > 1:
    sys.exit("duplicate api_server key under platform_toolsets")
if hits:
    n = hits[0]
    inline = block[n].split(":", 1)[1].strip()
    if inline:                       # e.g. "api_server: []"
        block[n:n + 1] = ["  api_server:", "    - hermes-api-server", "    - clarify"]
    else:
        block.insert(n + 1, "    - clarify")
else:
    block[0:0] = ["  api_server:", "    - hermes-api-server", "    - clarify"]
open(p, "w").write("\n".join(lines[:i + 1] + block + lines[end:]) + "\n")
PY
}

ORIG_MODE=$(cfg_mode)
cleanup() {
  cfg_set_mode "$ORIG_MODE"
  rm -f "$BACKUP"
  rm -rf "$PROBE_TMP"
}
trap cleanup EXIT INT TERM

if [ "$NO_CONFIG" != 1 ]; then
  cp "$CONFIG_FILE" "$BACKUP"
  if [ "$ORIG_MODE" != manual ]; then
    cfg_set_mode manual
  fi
  if ! cfg_toolsets_has_clarify; then
    cfg_toolsets_add_clarify
  fi
  sleep 1   # let the gateway's live config cache notice the mtime change
  echo "config: approvals.mode=$ORIG_MODE -> manual (restored on exit); clarify toolset enabled for api_server"
fi

# ---------------------------------------------------------------- helpers
probe() {
  local desc="$1" cmd="$2"
  printf '\n### %s\n$ %s\n' "$desc" "${cmd//__KEY__/\$HERMES_API_KEY}"
  printf -- '--- response ---\n'
  eval "${cmd//__KEY__/$KEY}"
  printf '\n'
}
json_field() { python3 -c "import json,sys;d=json.load(sys.stdin);print(eval(sys.argv[1]))" "$1"; }
wait_control() {  # $1 run id, $2 max seconds; prints poll lines, returns 0 once a control appears
  local rid="$1" deadline=$(( $(date +%s) + ${2:-120} )) body
  while [ "$(date +%s)" -lt "$deadline" ]; do
    body=$(curl -sS -H "Authorization: Bearer $KEY" "$BASE/v1/runs/$rid")
    python3 -c "
import json,sys
d=json.loads(sys.argv[1])
print('status=%s pending=%s' % (d.get('status'), json.dumps(d.get('pending_controls'))[:300]))
" "$body"
    case "$body" in *'"prompt_id"'*) return 0;; esac
    sleep 2
  done
  return 1
}

echo "Hermes API contract probe"
echo "base=$BASE"
echo

# ---------------------------------------------------------------- 1. auth
probe "unauthenticated request" \
  "curl -sS -w '\\nHTTP %{http_code}\\n' $BASE/v1/capabilities"
probe "wrong bearer token" \
  "curl -sS -w '\\nHTTP %{http_code}\\n' -H 'Authorization: Bearer not-the-key' $BASE/v1/capabilities"

# ---------------------------------------------------------------- 2. capabilities
echo
echo "### capabilities (full body, pretty-printed)"
echo "\$ curl -sS -H 'Authorization: Bearer \$HERMES_API_KEY' $BASE/v1/capabilities"
echo "--- response ---"
curl -sS -H "Authorization: Bearer $KEY" "$BASE/v1/capabilities" | python3 -m json.tool
echo

# ---------------------------------------------------------------- 3. run + live stream
echo
echo "### POST /v1/runs with an idempotency key, then stream its events"
RUN_JSON=$(curl -sS -D "$PROBE_TMP/run-headers.txt" -X POST \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -H "X-Hermes-Session-Key: voice-relay-probe" -H "Idempotency-Key: $IDEM_KEY" \
  -d '{"input":"Use your terminal tool to run the shell command `uname -s`, then reply with only the output."}' \
  "$BASE/v1/runs")
printf "\$ curl -sS -i -X POST -H 'Authorization: Bearer \$HERMES_API_KEY' -H 'Content-Type: application/json' -H 'X-Hermes-Session-Key: voice-relay-probe' -H 'Idempotency-Key: $IDEM_KEY' -d '{\"input\":\"Use your terminal tool to run the shell command \`uname -s\`, then reply with only the output.\"}' $BASE/v1/runs\n"
printf -- '--- response ---\n'
grep -i '^HTTP\|^x-hermes\|^idempotency' "$PROBE_TMP/run-headers.txt"
echo "$RUN_JSON"
RUN_ID=$(printf '%s' "$RUN_JSON" | json_field 'd["run_id"]')
echo "run_id=$RUN_ID"
echo
echo "### GET /v1/runs/$RUN_ID/events (SSE, live)"
echo "\$ curl -sS -N -H 'Authorization: Bearer \$HERMES_API_KEY' $BASE/v1/runs/$RUN_ID/events"
echo "--- response ---"
curl -sS -N --max-time "$SSE_WAIT" -H "Authorization: Bearer $KEY" "$BASE/v1/runs/$RUN_ID/events"
echo
echo

# ---------------------------------------------------------------- 4. idempotency replay/conflict
probe "same Idempotency-Key, identical payload (replay)" \
  "curl -sS -i -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -H 'X-Hermes-Session-Key: voice-relay-probe' -H 'Idempotency-Key: $IDEM_KEY' -d '{\"input\":\"Use your terminal tool to run the shell command \`uname -s\`, then reply with only the output.\"}' $BASE/v1/runs"
probe "same Idempotency-Key, different payload (conflict)" \
  "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -H 'X-Hermes-Session-Key: voice-relay-probe' -H 'Idempotency-Key: $IDEM_KEY' -d '{\"input\":\"something else entirely\"}' $BASE/v1/runs"

# ---------------------------------------------------------------- 5. approval flow
if [ "$NO_CONFIG" != 1 ]; then
  echo
  echo "### approval: run a dangerous command, wait for the prompt"
  APP_JSON=$(curl -sS -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    -d '{"input":"Use your terminal tool to run the shell command `rm -rf /tmp/hermes-api-probe-dir`, then reply with only the word done."}' \
    "$BASE/v1/runs")
  APP_ID=$(printf '%s' "$APP_JSON" | json_field 'd["run_id"]')
  printf "\$ curl -sS -X POST -H 'Authorization: Bearer \$HERMES_API_KEY' -H 'Content-Type: application/json' -d '{\"input\":\"Use your terminal tool to run the shell command \`rm -rf /tmp/hermes-api-probe-dir\`, then reply with only the word done.\"}' $BASE/v1/runs\n"
  echo "--- response ---"
  echo "$APP_JSON"
  echo
  echo "### GET /v1/runs/$APP_ID while the approval is pending (polled every 2s)"
  echo "\$ curl -sS -H 'Authorization: Bearer \$HERMES_API_KEY' $BASE/v1/runs/$APP_ID"
  echo "--- response ---"
  wait_control "$APP_ID" 180
  echo
  PENDING=$(curl -sS -H "Authorization: Bearer $KEY" "$BASE/v1/runs/$APP_ID")
  echo "--- full body while pending ---"
  printf '%s' "$PENDING" | python3 -m json.tool
  PROMPT_ID=$(printf '%s' "$PENDING" | json_field 'd["pending_controls"][0]["prompt_id"]')
  GEN=$(printf '%s' "$PENDING" | json_field 'd["pending_controls"][0]["execution_generation"]')
  echo
  probe "approval with an extra key (invalid_params)" \
    "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"request_id\":\"$PROMPT_ID\",\"execution_generation\":$GEN,\"choice\":\"once\",\"extra\":true}' $BASE/v1/runs/$APP_ID/approval"
  probe "approval with a stale execution_generation" \
    "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"request_id\":\"$PROMPT_ID\",\"execution_generation\":$((GEN+6)),\"choice\":\"once\"}' $BASE/v1/runs/$APP_ID/approval"
  probe "approval with an unknown request_id" \
    "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"request_id\":\"not-a-prompt\",\"execution_generation\":$GEN,\"choice\":\"once\"}' $BASE/v1/runs/$APP_ID/approval"
  probe "approval accepted" \
    "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"request_id\":\"$PROMPT_ID\",\"execution_generation\":$GEN,\"choice\":\"once\"}' $BASE/v1/runs/$APP_ID/approval"
  probe "same approval answered twice" \
    "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"request_id\":\"$PROMPT_ID\",\"execution_generation\":$GEN,\"choice\":\"once\"}' $BASE/v1/runs/$APP_ID/approval"
  echo
  echo "### event stream of the approval run (approval.request ordering)"
  echo "\$ curl -sS -N -H 'Authorization: Bearer \$HERMES_API_KEY' $BASE/v1/runs/$APP_ID/events"
  echo "--- response ---"
  curl -sS -N --max-time 30 -H "Authorization: Bearer $KEY" "$BASE/v1/runs/$APP_ID/events"

  echo
  echo "### clarify: ask the agent to use the clarify tool"
  CL_JSON=$(curl -sS -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    -d '{"input":"Use your clarify tool to ask me one question: which colour do you prefer, red or blue? Wait for my answer, then reply with only my chosen colour."}' \
    "$BASE/v1/runs")
  CL_ID=$(printf '%s' "$CL_JSON" | json_field 'd["run_id"]')
  printf "\$ curl -sS -X POST -H 'Authorization: Bearer \$HERMES_API_KEY' -H 'Content-Type: application/json' -d '{\"input\":\"Use your clarify tool to ask me one question: which colour do you prefer, red or blue? Wait for my answer, then reply with only my chosen colour.\"}' $BASE/v1/runs\n"
  echo "--- response ---"
  echo "$CL_JSON"
  echo
  echo "### GET /v1/runs/$CL_ID while the clarify prompt is open (polled every 2s)"
  echo "\$ curl -sS -H 'Authorization: Bearer \$HERMES_API_KEY' $BASE/v1/runs/$CL_ID"
  echo "--- response ---"
  wait_control "$CL_ID" 180
  echo
  CL_PENDING=$(curl -sS -H "Authorization: Bearer $KEY" "$BASE/v1/runs/$CL_ID")
  echo "--- full body while pending ---"
  printf '%s' "$CL_PENDING" | python3 -m json.tool
  CL_PROMPT=$(printf '%s' "$CL_PENDING" | json_field 'd["pending_controls"][0]["prompt_id"]')
  CL_GEN=$(printf '%s' "$CL_PENDING" | json_field 'd["pending_controls"][0]["execution_generation"]')
  echo
  probe "clarify with the wrong key name (invalid_params)" \
    "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"request_id\":\"$CL_PROMPT\",\"execution_generation\":$CL_GEN,\"choice\":\"Red\"}' $BASE/v1/runs/$CL_ID/clarify"
  probe "clarify with a stale execution_generation" \
    "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"request_id\":\"$CL_PROMPT\",\"execution_generation\":0,\"answer\":\"Red\"}' $BASE/v1/runs/$CL_ID/clarify"
  probe "clarify answered" \
    "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"request_id\":\"$CL_PROMPT\",\"execution_generation\":$CL_GEN,\"answer\":\"Red\"}' $BASE/v1/runs/$CL_ID/clarify"
  probe "same clarify answered twice" \
    "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"request_id\":\"$CL_PROMPT\",\"execution_generation\":$CL_GEN,\"answer\":\"Red\"}' $BASE/v1/runs/$CL_ID/clarify"
fi

# ---------------------------------------------------------------- 6. stop
echo
echo "### stop: interrupt a run that is sleeping"
STOP_JSON=$(curl -sS -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"input":"Use your terminal tool to run the shell command `sleep 45`, then reply with only the word done."}' \
  "$BASE/v1/runs")
STOP_ID=$(printf '%s' "$STOP_JSON" | json_field 'd["run_id"]')
printf "\$ curl -sS -X POST -H 'Authorization: Bearer \$HERMES_API_KEY' -H 'Content-Type: application/json' -d '{\"input\":\"Use your terminal tool to run the shell command \`sleep 45\`, then reply with only the word done.\"}' $BASE/v1/runs\n"
echo "--- response ---"
echo "$STOP_JSON"
sleep 15
probe "stop a running turn" \
  "curl -sS -w '\\nHTTP %{http_code} in %{time_total}s\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{}' $BASE/v1/runs/$STOP_ID/stop"
sleep 3
probe "status after stop" \
  "curl -sS -H 'Authorization: Bearer __KEY__' $BASE/v1/runs/$STOP_ID"
probe "stop an already-terminal run" \
  "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{}' $BASE/v1/runs/$STOP_ID/stop"
probe "stop an unknown run" \
  "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{}' $BASE/v1/runs/run_doesnotexist/stop"
echo
echo "### event stream after the stop (run.cancelled)"
echo "\$ curl -sS -N -H 'Authorization: Bearer \$HERMES_API_KEY' $BASE/v1/runs/$STOP_ID/events"
echo "--- response ---"
curl -sS -N --max-time 6 -H "Authorization: Bearer $KEY" "$BASE/v1/runs/$STOP_ID/events"

# ---------------------------------------------------------------- 7. sessions
echo
echo "### sessions: create"
TITLE="hermes-api probe $(date +%Y%m%d-%H%M%S)"
SESS_JSON=$(curl -sS -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d "{\"title\":\"$TITLE\"}" "$BASE/api/sessions")
echo "\$ curl -sS -X POST -H 'Authorization: Bearer \$HERMES_API_KEY' -H 'Content-Type: application/json' -d '{\"title\":\"$TITLE\"}' $BASE/api/sessions"
echo "--- response ---"
printf '%s' "$SESS_JSON" | python3 -m json.tool
SESS_ID=$(printf '%s' "$SESS_JSON" | json_field 'd["session"]["id"]')
echo "session_id=$SESS_ID"
probe "sessions: create with an already-used title" \
  "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"title\":\"$TITLE\"}' $BASE/api/sessions"
probe "sessions: list" \
  "curl -sS -H 'Authorization: Bearer __KEY__' '$BASE/api/sessions?limit=2'"
probe "sessions: get" \
  "curl -sS -H 'Authorization: Bearer __KEY__' $BASE/api/sessions/$SESS_ID"
probe "sessions: chat (one synchronous turn)" \
  "curl -sS -w '\\nHTTP %{http_code} in %{time_total}s\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"message\":\"Reply with only the word pong.\"}' $BASE/api/sessions/$SESS_ID/chat"
probe "sessions: messages" \
  "curl -sS -H 'Authorization: Bearer __KEY__' $BASE/api/sessions/$SESS_ID/messages"
probe "sessions: chat/stream" \
  "curl -sS -N --max-time 120 -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"message\":\"Reply with only the word ping.\"}' $BASE/api/sessions/$SESS_ID/chat/stream"
probe "sessions: patch title" \
  "curl -sS -w '\\nHTTP %{http_code}\\n' -X PATCH -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"title\":\"$TITLE renamed\"}' $BASE/api/sessions/$SESS_ID"
FORK_JSON=$(curl -sS -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{}' "$BASE/api/sessions/$SESS_ID/fork")
FORK_ID=$(printf '%s' "$FORK_JSON" | json_field 'd["session"]["id"]')
echo "\$ curl -sS -X POST -H 'Authorization: Bearer \$HERMES_API_KEY' -H 'Content-Type: application/json' -d '{}' $BASE/api/sessions/$SESS_ID/fork"
echo "--- response ---"
printf '%s' "$FORK_JSON" | python3 -m json.tool
echo "fork_session_id=$FORK_ID"
probe "sessions: model lock without provider" \
  "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"model\":\"hermes-agent\"}' $BASE/api/sessions/$SESS_ID/model"
probe "sessions: delete the forked session" \
  "curl -sS -w '\\nHTTP %{http_code}\\n' -X DELETE -H 'Authorization: Bearer __KEY__' $BASE/api/sessions/$FORK_ID"
probe "sessions: delete an unknown session" \
  "curl -sS -w '\\nHTTP %{http_code}\\n' -X DELETE -H 'Authorization: Bearer __KEY__' $BASE/api/sessions/api_doesnotexist"

# ---------------------------------------------------------------- 8. ownership
echo
echo "### surface ownership: bind a session owned by another surface"
FOREIGN=$(python3 -c "
import sqlite3,os
c=sqlite3.connect(os.path.expanduser('~/.hermes/state.db'))
print(c.execute(\"select id from sessions where source!='api_server' order by started_at desc limit 1\").fetchone()[0])
")
echo "# foreign session id discovered via sqlite (~/.hermes/state.db): $FOREIGN"
probe "POST /v1/runs bound to a foreign session" \
  "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"input\":\"hello\",\"session_id\":\"$FOREIGN\"}' $BASE/v1/runs"
probe "POST /api/sessions/{foreign}/chat" \
  "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"message\":\"hello\"}' $BASE/api/sessions/$FOREIGN/chat"

# ---------------------------------------------------------------- 9. attachments
echo
echo "### attachments: inline images only"
PNG='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
probe "chat/completions with a file part" \
  "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"model\":\"hermes-agent\",\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"what is in this file?\"},{\"type\":\"file\",\"file\":{\"filename\":\"notes.txt\",\"file_data\":\"data:text/plain;base64,aGVsbG8=\"}}]}]}' $BASE/v1/chat/completions"
probe "chat/completions with a non-image data URL" \
  "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"model\":\"hermes-agent\",\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"describe\"},{\"type\":\"image_url\",\"image_url\":{\"url\":\"data:text/plain;base64,aGVsbG8=\"}}]}]}' $BASE/v1/chat/completions"
probe "chat/completions with a bare relative path" \
  "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"model\":\"hermes-agent\",\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"describe\"},{\"type\":\"image_url\",\"image_url\":{\"url\":\"./photo.png\"}}]}]}' $BASE/v1/chat/completions"
probe "responses with an input_file part" \
  "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"model\":\"hermes-agent\",\"input\":[{\"role\":\"user\",\"content\":[{\"type\":\"input_text\",\"text\":\"summarise\"},{\"type\":\"input_file\",\"file_id\":\"file-abc123\"}]}]}' $BASE/v1/responses"
probe "chat/completions with an inline image (accepted)" \
  "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"model\":\"hermes-agent\",\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"Reply with only the word image.\"},{\"type\":\"image_url\",\"image_url\":{\"url\":\"$PNG\"}}]}]}' $BASE/v1/chat/completions"
probe "POST /v1/runs with an inline image (accepted)" \
  "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -d '{\"input\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"Reply with only the word image.\"},{\"type\":\"image_url\",\"image_url\":{\"url\":\"$PNG\"}}]}]}' $BASE/v1/runs"

# ---------------------------------------------------------------- 10. session key
echo
echo "### X-Hermes-Session-Key"
probe "declared session key is echoed on admission" \
  "curl -sS -i -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -H 'X-Hermes-Session-Key: voice-relay-probe' -d '{\"input\":\"Reply with only the word keyed.\"}' $BASE/v1/runs"
LONG=$(python3 -c "print('k'*300)")
probe "session key over the length limit" \
  "curl -sS -w '\\nHTTP %{http_code}\\n' -X POST -H 'Authorization: Bearer __KEY__' -H 'Content-Type: application/json' -H 'X-Hermes-Session-Key: $LONG' -d '{\"input\":\"hi\"}' $BASE/v1/runs"
echo
echo "### session key containing a NUL byte (raw socket; curl cannot carry it)"
echo "\$ raw HTTP/1.1 request with header 'X-Hermes-Session-Key: bad\\x00key' to $BASE"
echo "--- response ---"
python3 - "$KEY" "$BASE" <<'PY'
import socket, sys
key, base = sys.argv[1], sys.argv[2]
host, port = base.replace("http://", "").split(":")
body = b'{"input":"hi"}'
req = (b"POST /v1/runs HTTP/1.1\r\nHost: " + base.replace("http://", "").encode()
       + b"\r\nAuthorization: Bearer " + key.encode()
       + b"\r\nContent-Type: application/json\r\nX-Hermes-Session-Key: bad\x00key\r\n"
       + b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
s = socket.create_connection((host, int(port)))
s.sendall(req)
data = b""
while True:
    chunk = s.recv(65536)
    if not chunk:
        break
    data += chunk
head, _, rest = data.partition(b"\r\n\r\n")
print(head.decode("utf-8", "replace"))
print("---")
print(rest.decode("utf-8", "replace"))
PY

# ---------------------------------------------------------------- 11. durable status vs swept buffer
echo
echo "### durable status vs. swept event buffer"
probe "GET status of the earlier run" \
  "curl -sS -H 'Authorization: Bearer __KEY__' $BASE/v1/runs/$RUN_ID"
probe "GET events of the same run" \
  "curl -sS -w '\\nHTTP %{http_code}\\n' --max-time 6 -H 'Authorization: Bearer __KEY__' $BASE/v1/runs/$RUN_ID/events"

echo
echo "probe complete"
