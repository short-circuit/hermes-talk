#!/usr/bin/env bash
# Smoke-test the Hermes API server for the relay's needs.
#
# Checks, against a live gateway, in order:
#   1. the loopback listener and unauthenticated /health;
#   2. bearer auth is enforced (an unauthenticated call is refused);
#   3. GET /v1/capabilities exposes every flag the relay gates on;
#   4. POST /v1/runs admits a tool-using turn (202 + run_id);
#   5. GET /v1/runs/{id}/events streams tool.started, tool.completed and run.completed in seq order;
#   6. GET /v1/runs/{id} reports the terminal status of that run.
#
# It never restarts the gateway. It creates one api_server session for the smoke turn and
# deletes it at the end (set HERMES_SMOKE_KEEP=1 to keep it).
#
# Prerequisites: the gateway is running with platforms.api_server.enabled: true and an
# API_SERVER_KEY of at least 16 characters, and the configured model provider is local.
# The bearer token is printed as $HERMES_API_KEY; its value never appears in the output.
#
# Usage:
#   HERMES_API_KEY=... scripts/smoke_hermes_api.sh 2>&1 | tee /tmp/hermes-api-smoke.txt

set -uo pipefail

BASE=${HERMES_API_BASE:-http://127.0.0.1:8642}
ENV_FILE=${HERMES_ENV_FILE:-$HOME/.hermes/.env}
WAIT=${HERMES_SMOKE_WAIT:-180}
KEEP=${HERMES_SMOKE_KEEP:-0}
TMP=$(mktemp -d)
FAIL=0
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

KEY=${HERMES_API_KEY:-}
if [ -z "$KEY" ] && [ -f "$ENV_FILE" ]; then
  KEY=$(python3 -c "import re,sys;m=re.search(r'^API_SERVER_KEY=(.*)\$',open(sys.argv[1]).read(),re.M);print(m.group(1).strip() if m else '')" "$ENV_FILE")
fi
if [ -z "$KEY" ]; then
  echo "no API key: set HERMES_API_KEY or put API_SERVER_KEY in $ENV_FILE" >&2
  exit 1
fi

check() { # check <label> <condition-exit-code>
  if [ "$2" -eq 0 ]; then printf 'PASS  %s\n' "$1"; else printf 'FAIL  %s\n' "$1"; FAIL=1; fi
}

echo "== 1. listener and unauthenticated health"
echo "\$ ss -ltn | grep 8642"
ss -ltn 2>/dev/null | grep 8642 || echo "(ss unavailable: skipping listener check)"
echo
echo "\$ curl -sS -w '\\nHTTP %{http_code}\\n' $BASE/health"
curl -sS -m 10 -w '\nHTTP %{http_code}\n' "$BASE/health"
H_CODE=$(curl -sS -m 10 -o "$TMP/health.json" -w '%{http_code}' "$BASE/health")
check "GET /health -> 200" "$([ "$H_CODE" = "200" ] && echo 0 || echo 1)"
check "/health reports status ok" "$(python3 -c "import json,sys;print(0 if json.load(open('$TMP/health.json')).get('status')=='ok' else 1)")"

echo
echo "== 2. bearer auth is enforced"
echo "\$ curl -sS -w '\\nHTTP %{http_code}\\n' $BASE/v1/capabilities   # no Authorization header"
curl -sS -m 10 -w '\nHTTP %{http_code}\n' "$BASE/v1/capabilities"
A_CODE=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' "$BASE/v1/capabilities")
check "GET /v1/capabilities without a key -> 401" "$([ "$A_CODE" = "401" ] && echo 0 || echo 1)"

echo
echo "== 3. capabilities"
echo "\$ curl -sS -H 'Authorization: Bearer \$HERMES_API_KEY' $BASE/v1/capabilities"
curl -sS -m 10 -H "Authorization: Bearer $KEY" "$BASE/v1/capabilities" -o "$TMP/caps.json"
C_CODE=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $KEY" "$BASE/v1/capabilities")
check "GET /v1/capabilities -> 200" "$([ "$C_CODE" = "200" ] && echo 0 || echo 1)"
echo
echo "required feature flags:"
jq -r '.features | to_entries[] | select(.key | test("^(run_submission|run_status|run_events_sse|run_stop|run_steer|run_approval_response|tool_progress_events|approval_events|session_resources|session_chat|session_chat_stream|session_fork|session_model_lock|runs_idempotency|session_key_header|session_continuity_header|cors)$")) | "\(.key) = \(.value | tojson)"' "$TMP/caps.json"
echo
MISSING=$(jq -r '[.features | to_entries[] | select(.key as $k | ["run_submission","run_status","run_events_sse","run_stop","run_steer","run_approval_response","tool_progress_events","approval_events","session_resources","session_chat","session_chat_stream","session_fork","session_model_lock"] | index($k)) | select(.value != true) | .key] | join(",")' "$TMP/caps.json")
check "every gated feature flag is true (missing: ${MISSING:-none})" "$([ -z "$MISSING" ] && echo 0 || echo 1)"
check "features.cors is false (loopback-only client)" "$(jq -e '.features.cors == false' "$TMP/caps.json" >/dev/null && echo 0 || echo 1)"
check "features.audio_api is false (speech stays in the relay)" "$(jq -e '.features.audio_api == false' "$TMP/caps.json" >/dev/null && echo 0 || echo 1)"

echo
echo "== 4. admit one tool-using run"
SKEY="relay-smoke-$(date +%s)-$$"
IDEM="smoke-$(date +%s)-$$"
echo "\$ curl -sS -i -X POST -H 'Authorization: Bearer \$HERMES_API_KEY' -H 'Content-Type: application/json' \\"
echo "    -H 'X-Hermes-Session-Key: $SKEY' -H 'Idempotency-Key: $IDEM' \\"
echo "    -d '{\"input\":\"Use your terminal tool to run the shell command \`uname -s\`, then reply with only the output.\"}' \\"
echo "    $BASE/v1/runs"
curl -sS -m 30 -D "$TMP/admit.headers" -o "$TMP/admit.json" -w '\nHTTP %{http_code}\n' \
  -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -H "X-Hermes-Session-Key: $SKEY" -H "Idempotency-Key: $IDEM" \
  -d '{"input":"Use your terminal tool to run the shell command `uname -s`, then reply with only the output."}' \
  "$BASE/v1/runs"
cat "$TMP/admit.json"; echo
grep -i '^x-hermes-session-key' "$TMP/admit.headers" || true
RUN=$(jq -r '.run_id // empty' "$TMP/admit.json")
check "POST /v1/runs -> run_id present" "$([ -n "$RUN" ] && echo 0 || echo 1)"
if [ -z "$RUN" ]; then echo; echo "no run id; aborting"; exit 1; fi

echo
echo "== 5. event stream (SSE)"
echo "\$ curl -sS -N --max-time $WAIT -H 'Authorization: Bearer \$HERMES_API_KEY' $BASE/v1/runs/$RUN/events"
curl -sS -N --max-time "$WAIT" -H "Authorization: Bearer $KEY" "$BASE/v1/runs/$RUN/events" -o "$TMP/events.txt"
cat "$TMP/events.txt"
echo
python3 - "$TMP/events.txt" <<'PY'
import json, sys
seqs, names, terminal = [], [], None
for line in open(sys.argv[1]):
    line = line.rstrip("\n")
    if line.startswith("data: "):
        try:
            ev = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        seqs.append(ev.get("seq"))
        names.append(ev.get("event"))
        if ev.get("event") in ("run.completed", "run.cancelled", "run.failed"):
            terminal = ev
print()
print("events:", ", ".join(f"{s}:{n}" for s, n in zip(seqs, names)))
ok = True
if seqs != sorted(seqs):
    print("FAIL  seq numbers are not monotonic"); ok = False
if "tool.started" not in names or "tool.completed" not in names:
    print("FAIL  tool.started / tool.completed missing"); ok = False
if terminal is None or terminal.get("event") != "run.completed":
    print("FAIL  terminal frame is not run.completed"); ok = False
if terminal and terminal.get("completed") is not True:
    print("FAIL  run.completed.completed is not true"); ok = False
print("PASS  ordered tool events and run.completed terminal frame" if ok else "")
sys.exit(0 if ok else 1)
PY
check "SSE ordering and terminal frame" "$?"

echo
echo "== 6. terminal status"
echo "\$ curl -sS -H 'Authorization: Bearer \$HERMES_API_KEY' $BASE/v1/runs/$RUN"
curl -sS -m 10 -H "Authorization: Bearer $KEY" "$BASE/v1/runs/$RUN" -o "$TMP/status.json" -w '\nHTTP %{http_code}\n'
cat "$TMP/status.json"; echo
check "GET /v1/runs/{id} -> completed" "$(jq -e '.status == "completed"' "$TMP/status.json" >/dev/null && echo 0 || echo 1)"
SID=$(jq -r '.session_id // empty' "$TMP/status.json")
echo "run_id=$RUN session_id=${SID:-unknown}"

echo
if [ "$KEEP" = "1" ] || [ -z "$SID" ]; then
  echo "kept session ${SID:-unknown} (HERMES_SMOKE_KEEP=$KEEP)"
else
  echo "== 7. delete the smoke session"
  echo "\$ curl -sS -w '\\nHTTP %{http_code}\\n' -X DELETE -H 'Authorization: Bearer \$HERMES_API_KEY' $BASE/api/sessions/$SID"
  curl -sS -m 10 -w '\nHTTP %{http_code}\n' -X DELETE -H "Authorization: Bearer $KEY" "$BASE/api/sessions/$SID"
  echo
fi

echo
if [ "$FAIL" -eq 0 ]; then echo "RESULT: smoke test passed"; exit 0; else echo "RESULT: smoke test FAILED"; exit 1; fi
