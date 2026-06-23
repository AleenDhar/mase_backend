#!/usr/bin/env bash
# =============================================================================
# MASE backend smoke test  —  run BEFORE and AFTER every deploy.
#
# Confirms every critical route is LIVE (registered + not server-erroring) and
# that the durable ENV (datalake / SNS / LLM tuning) survived the deploy. It uses
# SAFE probes only: GETs, and POSTs with an empty body that the handler rejects
# with 400/422 — so it proves "the route exists" WITHOUT running a sweep, a chat,
# or any write. Nothing here mutates data.
#
# PASS = status is NOT 404 (route missing) and NOT 5xx (server error).
#        200 / 400 / 401 / 403 / 422 all mean "route is alive".
#
# Usage:
#   BASE_URL=http://mase-alb-...elb.amazonaws.com \
#   TOKEN=<DEAL_ENGINE_TOKEN> \
#   ./scripts/smoke_test.sh
#
#   # typical pre/post-deploy:
#   ./scripts/smoke_test.sh && echo "PRE-DEPLOY OK"     # before deploy.ps1
#   ./scripts/smoke_test.sh && echo "POST-DEPLOY OK"    # after  deploy.ps1
#
# Exit code 0 = all good; 1 = at least one route is 404/5xx (DO NOT ship / ROLL BACK).
# =============================================================================
set -uo pipefail

BASE="${BASE_URL:-http://mase-alb-1262623499.ap-south-1.elb.amazonaws.com}"
TOKEN="${TOKEN:-${DEAL_ENGINE_TOKEN:-}}"
if [[ -z "$TOKEN" ]]; then echo "ERROR: set TOKEN or DEAL_ENGINE_TOKEN env"; exit 2; fi

PASS=0; FAIL=0; FAILED_LIST=()

# probe <METHOD> <PATH> <BODY-or-->  <human label>
probe() {
  local method="$1" path="$2" body="$3" label="$4" code
  if [[ "$body" != "-" ]]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' -X "$method" "$BASE$path" \
      -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "$body" --max-time 30)
  else
    code=$(curl -s -o /dev/null -w '%{http_code}' -X "$method" "$BASE$path" \
      -H "Authorization: Bearer $TOKEN" --max-time 30)
  fi
  if [[ "$code" == "404" || "$code" == "000" || "$code" -ge 500 ]]; then
    printf '  FAIL  %-6s %-46s -> %s   (%s)\n' "$method" "$path" "$code" "$label"
    FAIL=$((FAIL+1)); FAILED_LIST+=("$method $path -> $code")
  else
    printf '  ok    %-6s %-46s -> %s\n' "$method" "$path" "$code"
    PASS=$((PASS+1))
  fi
}

echo "=== MASE smoke test  @ $BASE ==="

echo "-- core / health --"
probe GET  /api/health                                  -    "process + agent + MCP up"
probe GET  /api/deal-engine/health                      -    "deal-engine health"
probe GET  /api/deal-engine/selfcheck                   -    "ENV self-check (datalake/SNS/LLM)"
probe GET  /api/tools                                   -    "agent tools loaded"
probe GET  /api/mcp/servers                             -    "MCP servers"

echo "-- chat (the recurring 404 regression) --"
probe POST /api/deal-engine/chat/async       '{}'            "RevOps chat (async) — MUST NOT be 404"
probe POST /api/deal-engine/chat             '{}'            "RevOps chat (sync) — MUST NOT be 404"
probe GET  /api/deal-engine/chat/prompt      -               "chat prompt editor"
probe POST /api/deal-engine/chat/stop        '{}'            "chat stop"

echo "-- deal book / opportunities --"
probe GET  /api/deal-engine/opportunities?slim=1   -        "opportunity book"
probe GET  /api/deal-engine/deals-count            -        "deals count"
probe GET  /api/deal-engine/team                   -        "team roster"
probe GET  /api/deal-engine/matcha                 -        "matcha view"
probe GET  /api/deal-engine/deltas                 -        "what-changed deltas"

echo "-- sweep / engine --"
probe GET  /api/deal-engine/sweep/status           -        "sweep queue status"
probe GET  /api/deal-engine/sweep/prompt           -        "sweep prompt editor"
probe POST /api/deal-engine/sweep/trigger    '{}'           "sweep trigger (validates empty -> 400)"
probe GET  /api/deal-engine/hard-refresh/status    -        "hard-refresh status"
probe GET  /api/deal-engine/trigger-logs           -        "trigger logs (Avoma)"

echo "-- to-dos / updates (the Add-update path) --"
probe GET  /api/deal-engine/todo                   -        "to-do book"
probe POST /api/deal-engine/todo/update      '{}'           "add next-step/todo/completed (validates -> 400)"
probe POST /api/deal-engine/todo/push        '{}'           "push to Salesforce (validates -> 400)"

echo "-- learnings / knowledge --"
probe GET  /api/deal-engine/learnings              -        "learning observatory"
probe GET  /api/deal-engine/knowledge              -        "knowledge store"

echo "-- webhook (Avoma -> datalake) --"
probe POST /webhook                          '{}'           "SNS/Avoma webhook receiver (validates -> 400)"

echo
echo "=== RESULT: $PASS passed, $FAIL failed ==="
if [[ $FAIL -gt 0 ]]; then
  echo "FAILED ROUTES (404/5xx = broken or missing — DO NOT keep this build live, ROLL BACK):"
  for f in "${FAILED_LIST[@]}"; do echo "   - $f"; done
  exit 1
fi
echo "All critical routes live. Safe."
exit 0
