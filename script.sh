#!/usr/bin/env bash
# Smoke-test Calling Agent API: health, queued SMS/email, optional call.
#
# Prerequisites (local):
#   Terminal 1: uvicorn main:app --reload --port 8000
#   Terminal 2: arq messaging_oncall_worker.MessagingOnDemandWorkerSettings
#   Terminal 3: arq messaging_campaign_worker.MessagingCampaignWorkerSettings
#
# Usage:
#   chmod +x script.sh
#   BASE_URL=http://localhost:8000 SMS_TO=+91... EMAIL_TO=you@example.com EMAIL_FROM="Name <from@domain.com>" ./script.sh
#   DRY_RUN=1 ./script.sh   # health only

set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
DRY_RUN="${DRY_RUN:-0}"
POLL_INTERVAL="${POLL_INTERVAL:-2}"
POLL_MAX="${POLL_MAX:-30}"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

pass() { echo -e "${GREEN}✓${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }

json_get() {
  local expr="$1"
  if command -v jq >/dev/null 2>&1; then
    jq -r "$expr"
  else
    python3 - "$expr" <<'PY'
import json, sys
expr = sys.argv[1]
d = json.load(sys.stdin)
if expr == ".status":
    print(d.get("status", ""))
elif expr == ".ok":
    print(d.get("ok", False))
elif expr == ".task_token":
    print(d.get("task_token", ""))
else:
    print("")
PY
  fi
}

poll_message_status() {
  local token="$1"
  local elapsed=0
  local status=""
  while [ "$elapsed" -lt "$POLL_MAX" ]; do
    resp=$(curl -sS "${BASE_URL}/message/status/${token}")
    status=$(echo "$resp" | json_get ".status")
    echo "    status=${status} (${elapsed}s)"
    case "$status" in
      sent|failed|expired) echo "$status"; return 0 ;;
    esac
    sleep "$POLL_INTERVAL"
    elapsed=$((elapsed + POLL_INTERVAL))
  done
  echo "timeout"
  return 1
}

echo "=== Calling Agent smoke tests ==="
echo "BASE_URL=${BASE_URL}"
echo ""

# 1. Health
echo "[1] GET /health"
resp=$(curl -sS "${BASE_URL}/health")
ok=$(echo "$resp" | json_get ".ok")
if [ "$ok" = "true" ]; then
  pass "health ok"
else
  fail "health check failed: $resp"
fi

if [ "$DRY_RUN" = "1" ]; then
  echo "DRY_RUN=1 — skipping send tests"
  exit 0
fi

if [ -z "${SMS_TO:-}" ]; then
  fail "SMS_TO is required (E.164, e.g. +918102244713)"
fi
if [ -z "${EMAIL_TO:-}" ]; then
  fail "EMAIL_TO is required"
fi
if [ -z "${EMAIL_FROM:-}" ]; then
  fail "EMAIL_FROM is required (verified Resend sender)"
fi

# 2. SMS campaign
echo "[2] POST /sms/send (campaign)"
resp=$(curl -sS -X POST "${BASE_URL}/sms/send" \
  -H 'Content-Type: application/json' \
  -d "{\"to\":\"${SMS_TO}\",\"message\":\"Campaign SMS test $(date +%s)\",\"message_type\":\"campaign\"}")
status=$(echo "$resp" | json_get ".status")
task_token=$(echo "$resp" | json_get ".task_token")
if [ "$status" = "queued" ] && [ -n "$task_token" ] && [ "$task_token" != "null" ]; then
  pass "SMS campaign queued ($task_token)"
else
  fail "SMS campaign enqueue failed: $resp"
fi
final=$(poll_message_status "$task_token") || true
if [ "$final" = "sent" ]; then
  pass "SMS campaign delivered"
else
  fail "SMS campaign did not reach sent (got: $final)"
fi

# 3. SMS on-demand
echo "[3] POST /sms/send (on_demand)"
http_code=$(curl -sS -o /tmp/sms_od.json -w '%{http_code}' -X POST "${BASE_URL}/sms/send" \
  -H 'Content-Type: application/json' \
  -d "{\"to\":\"${SMS_TO}\",\"message\":\"On-demand SMS test $(date +%s)\",\"message_type\":\"on_demand\"}")
resp=$(cat /tmp/sms_od.json)
if [ "$http_code" = "200" ]; then
  od_status=$(echo "$resp" | json_get ".status")
  if [ "$od_status" = "sent" ]; then
    pass "SMS on-demand sent"
  else
    fail "SMS on-demand unexpected body: $resp"
  fi
elif [ "$http_code" = "503" ]; then
  pass "SMS on-demand 503 (slots busy — acceptable)"
else
  fail "SMS on-demand failed HTTP $http_code: $resp"
fi

# 4. Email campaign
echo "[4] POST /email/send (campaign)"
resp=$(curl -sS -X POST "${BASE_URL}/email/send" \
  -H 'Content-Type: application/json' \
  -d "{\"to\":\"${EMAIL_TO}\",\"from\":\"${EMAIL_FROM}\",\"subject\":\"Campaign email test\",\"body\":\"<p>Hello $(date +%s)</p>\",\"message_type\":\"campaign\"}")
status=$(echo "$resp" | json_get ".status")
task_token=$(echo "$resp" | json_get ".task_token")
if [ "$status" = "queued" ] && [ -n "$task_token" ] && [ "$task_token" != "null" ]; then
  pass "Email campaign queued ($task_token)"
else
  fail "Email campaign enqueue failed: $resp"
fi
final=$(poll_message_status "$task_token") || true
if [ "$final" = "sent" ]; then
  pass "Email campaign delivered"
else
  fail "Email campaign did not reach sent (got: $final)"
fi

# 5. Email on-demand
echo "[5] POST /email/send (on_demand)"
http_code=$(curl -sS -o /tmp/email_od.json -w '%{http_code}' -X POST "${BASE_URL}/email/send" \
  -H 'Content-Type: application/json' \
  -d "{\"to\":\"${EMAIL_TO}\",\"from\":\"${EMAIL_FROM}\",\"subject\":\"On-demand email test\",\"body\":\"<p>Hi $(date +%s)</p>\",\"message_type\":\"on_demand\"}")
resp=$(cat /tmp/email_od.json)
if [ "$http_code" = "200" ]; then
  od_status=$(echo "$resp" | json_get ".status")
  if [ "$od_status" = "sent" ]; then
    pass "Email on-demand sent"
  else
    fail "Email on-demand unexpected body: $resp"
  fi
elif [ "$http_code" = "503" ]; then
  pass "Email on-demand 503 (slots busy — acceptable)"
else
  fail "Email on-demand failed HTTP $http_code: $resp"
fi

# 6. Optional call campaign
if [ -n "${CALL_TO:-}" ]; then
  echo "[6] POST /call/outbound (campaign)"
  resp=$(curl -sS -X POST "${BASE_URL}/call/outbound" \
    -H 'Content-Type: application/json' \
    -d "{\"to\":\"${CALL_TO}\",\"call_type\":\"campaign\",\"name\":\"Test\"}")
  call_status=$(echo "$resp" | json_get ".status")
  if [ "$call_status" = "queued" ]; then
    pass "Call campaign queued"
  else
    fail "Call campaign failed: $resp"
  fi
fi

echo ""
echo "All tests passed."
