"""Focused status: Etex Group + NORTHPORT — queue row, run history, live record, deploy state."""
import sys, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"
cfg = {}
for _l in open(ENV, encoding="utf-8"):
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
K = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": K, "Authorization": f"Bearer {K}"}
API = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
AH = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}", "Content-Type": "application/json"}

TWO = [("Etex Group", "006P700000UGPE5"), ("NORTHPORT", "006P700000QFJwD")]

for lbl, oid in TWO:
    print("=" * 92)
    print(f"### {lbl}  ({oid})")
    q = requests.get(f"{SB}/rest/v1/sweep_queue",
                     params={"select": "status,attempts,error,duration_ms,claimed_at,updated_at",
                             "opp_id": f"eq.{oid}"}, headers=SH, verify=False, timeout=60).json()
    if q:
        x = q[0]
        print(f"  queue   : status={x['status']} attempts={x['attempts']} "
              f"claimed={str(x.get('claimed_at'))[11:19]} upd={str(x.get('updated_at'))[11:19]}")
        if x.get("error"):
            print(f"            err={str(x['error'])[:80]}")
    else:
        print("  queue   : (no row)")

    r = requests.get(f"{SB}/rest/v1/deal_records",
                     params={"select": "updated_at,scores:record->ai->deal_scores,"
                                       "studio:record->ai->scoring_studio,"
                                       "cov:record->evidence_coverage", "opp_id": f"eq.{oid}"},
                     headers=SH, verify=False, timeout=60).json()
    if r:
        r = r[0]
        ds = r.get("scores") or {}
        hl = ds.get("headline") or {}
        sv = (r.get("studio") or {}).get("versions") or {}
        cov = r.get("cov") or {}
        state = "HEALTHY" if ds.get("factor_source") == "ai" else (
            "SCORES WIPED (deal_scores null)" if not ds else "DEGRADED")
        print(f"  record  : {state}")
        print(f"            updated={str(r.get('updated_at'))[:19]} win={hl.get('win_position')} "
              f"mom={hl.get('deal_momentum')} src={ds.get('factor_source')} "
              f"winEng=v{sv.get('win')} calls_read={cov.get('calls_read')}")
    runs = requests.get(f"{SB}/rest/v1/deal_trigger_runs",
                        params={"select": "status,error,duration_ms,model,cost_usd,created_at,source",
                                "opp_id": f"eq.{oid}", "created_at": "gte.2026-07-09T14:00:00",
                                "order": "created_at.asc"},
                        headers=SH, verify=False, timeout=60).json()
    print("  runs today (since trigger):")
    for x in (runs or []):
        m = "sonnet-5" if "sonnet-5" in (x.get("model") or "") else "sonnet-4-5"
        d = x.get("duration_ms") or 0
        print(f"    {str(x['created_at'])[11:19]} {x['status']:>10} {d/60000:>5.1f}m "
              f"{m:11} src={x.get('source'):18} "
              f"{('err=' + str(x.get('error'))[:50]) if x.get('error') else ''}")
    print()

# Has the rollback (c91e808) landed? Old code enqueues (writes a sweep_queue row);
# rolled-back code returns "accepted" synchronously from trigger_opp_async. A bogus id
# distinguishes them at zero LLM cost: enqueue_trigger awaits is_active_member ->
# "not_in_book"; trigger_opp_async returns "accepted" immediately.
PROBE = "006000000000000AAA"
try:
    resp = requests.post(f"{API}/api/deal-engine/sweep/trigger", headers=AH,
                         json={"opp_id": PROBE, "source": "manual"}, verify=False, timeout=40)
    res = ((resp.json() or {}).get("results") or {}).get(PROBE)
    landed = (res == "accepted")
    print("=" * 92)
    print(f"ROLLBACK DEPLOY: probe={res!r} -> "
          f"{'LANDED (in-process path live, safe to re-sweep)' if landed else 'NOT YET (queue path still serving)'}")
except Exception as e:
    print(f"probe error: {type(e).__name__}: {e}")

try:
    h = requests.get(f"{API}/api/health", verify=False, timeout=30).json()
    srv = h.get("mcp_servers") or {}
    print(f"MCP ready      : tools={h.get('mcp_tools_loaded')} sf={srv.get('salesforce')} "
          f"avoma={srv.get('avoma')}")
except Exception as e:
    print(f"health error: {type(e).__name__}")
