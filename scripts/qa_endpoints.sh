#!/usr/bin/env bash
# =============================================================================
# Comprehensive post-deploy endpoint QA — the "a teammate can't silently break an
# endpoint" gate. Driven off the LIVE /openapi.json (auto-covers every route, never
# goes stale), it does three things:
#
#   1. ROUTE-SET DIFF vs a committed baseline (scripts/expected_routes.txt):
#      every route in the baseline MUST still exist in the live build. A removed /
#      renamed / un-registered route => FAIL (this is the teammate-broke-it catch).
#   2. CRASH PROBE: every non-parametrised route is hit (GET, or POST {} that the
#      handler validate-rejects) and must NOT return 5xx — catches a handler that
#      blew up at import/runtime.
#   3. ENV GATE: /selfcheck.ok must be true (datalake/SNS/LLM env not dropped).
#
# Safe: only GETs + empty-body POSTs (validate-reject), never DELETE / cron / a
# real sweep/chat. Exit 1 => the CI deploy fails and auto-rolls-back.
#
# Usage:  BASE_URL=... TOKEN=<DEAL_ENGINE_TOKEN> ./scripts/qa_endpoints.sh
# Regenerate the baseline (after intentionally adding/removing routes):
#         BASE_URL=... TOKEN=... ./scripts/qa_endpoints.sh --write-baseline
# =============================================================================
set -uo pipefail

BASE="${BASE_URL:-http://mase-alb-1262623499.ap-south-1.elb.amazonaws.com}"
TOKEN="${TOKEN:-${DEAL_ENGINE_TOKEN:-}}"
DIR="$(cd "$(dirname "$0")" && pwd)"
BASELINE="$DIR/expected_routes.txt"
if [[ -z "$TOKEN" ]]; then echo "ERROR: set TOKEN or DEAL_ENGINE_TOKEN"; exit 2; fi

# --- live route set from the FastAPI OpenAPI spec ----------------------------
SPEC=$(curl -s "$BASE/openapi.json" -H "Authorization: Bearer $TOKEN")
LIVE=$(echo "$SPEC" | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit('could not parse /openapi.json')
for p,ms in (d.get('paths') or {}).items():
    for m in ms:
        if m.lower() in ('get','post','put','delete','patch'): print(m.upper(), p)
" | tr -d '\r' | sort -u)
N=$(echo "$LIVE" | grep -c . || true)
if [[ "$N" -lt 20 ]]; then echo "FAIL: /openapi.json returned only $N routes — app likely unhealthy"; exit 1; fi
echo "live routes: $N"

# --- mode: write the baseline ------------------------------------------------
if [[ "${1:-}" == "--write-baseline" ]]; then
  echo "$LIVE" > "$BASELINE"
  echo "wrote baseline ($N routes) -> $BASELINE"
  exit 0
fi

FAIL=0

# 1) every baseline route must still exist
if [[ -f "$BASELINE" ]]; then
  MISSING=$(comm -23 <(tr -d '\r' < "$BASELINE" | sort -u) <(echo "$LIVE") || true)
  if [[ -n "$MISSING" ]]; then
    echo "FAIL: routes in the baseline are GONE from this build (removed/renamed/broken):"
    echo "$MISSING" | sed 's/^/   - /'
    FAIL=1
  else
    echo "ok: all $(grep -c . "$BASELINE") baseline routes present"
  fi
else
  echo "WARN: no baseline at $BASELINE — run with --write-baseline to create one"
fi

# 2) crash-probe every non-parametrised, non-destructive route
PROBED=0; CRASHED=0
while read -r METHOD PATHX; do
  [[ -z "${METHOD:-}" ]] && continue
  case "$PATHX" in *"{"*) continue ;; esac     # parametrised — can't safely probe
  case "$PATHX" in /cron/*) continue ;; esac    # cron jobs have side effects
  case "$METHOD" in DELETE) continue ;; esac     # destructive
  if [[ "$METHOD" == "GET" ]]; then
    CODE=$(curl -s -o /dev/null -w '%{http_code}' "$BASE$PATHX" -H "Authorization: Bearer $TOKEN" --max-time 25 || echo 000)
  else
    CODE=$(curl -s -o /dev/null -w '%{http_code}' -X "$METHOD" "$BASE$PATHX" \
      -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}' --max-time 25 || echo 000)
  fi
  PROBED=$((PROBED+1))
  if [[ "$CODE" == "000" ]] || { [[ "$CODE" =~ ^[0-9]+$ ]] && [[ "$CODE" -ge 500 ]]; }; then
    echo "FAIL: $METHOD $PATHX -> $CODE (server error)"
    CRASHED=$((CRASHED+1)); FAIL=1
  fi
done <<< "$LIVE"
echo "crash-probed $PROBED non-param routes; $CRASHED server-error(s)"

# 3) chat-404 explicit guard (the recurring outage)
C=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/deal-engine/chat/async" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}' --max-time 20 || echo 000)
if [[ "$C" == "404" || "$C" == "000" ]]; then echo "FAIL: chat/async -> $C (must not be 404)"; FAIL=1; else echo "ok: chat/async -> $C"; fi

# 4) env self-check gate
SC=$(curl -s "$BASE/api/deal-engine/selfcheck" -H "Authorization: Bearer $TOKEN")
if echo "$SC" | python3 -c "import json,sys;sys.exit(0 if json.load(sys.stdin).get('ok') else 1)"; then
  echo "ok: selfcheck.ok=true"
else
  echo "FAIL: selfcheck.ok=false -> $SC"; FAIL=1
fi

echo "============================================================"
if [[ $FAIL -eq 0 ]]; then echo "QA PASS — all endpoints accounted for and healthy"; else echo "QA FAIL — do not keep this build live (CI rolls back)"; fi
exit $FAIL
