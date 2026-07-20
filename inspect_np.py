"""Read-only deep look at one deal_records row + its queue row + run log."""
import sys, json, warnings
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
SH = {"apikey": cfg["SUPABASE_SERVICE_ROLE_KEY"],
      "Authorization": f"Bearer {cfg['SUPABASE_SERVICE_ROLE_KEY']}"}

OID = sys.argv[1] if len(sys.argv) > 1 else "006P700000QFJwD"

r = requests.get(f"{SB}/rest/v1/deal_records",
                 params={"select": "updated_at,swept_at,scores:record->ai->deal_scores,"
                                   "studio:record->ai->scoring_studio",
                         "opp_id": f"eq.{OID}"},
                 headers=SH, verify=False, timeout=(10, 60)).json()
if not r:
    print("NO ROW"); raise SystemExit
r = r[0]
ds = r.get("scores") or {}
print("updated_at      :", r.get("updated_at"))
print("swept_at        :", r.get("swept_at"))
print("deal_scores keys:", sorted(ds.keys()) if ds else "(EMPTY / null)")
print("headline        :", json.dumps(ds.get("headline"), default=str)[:220])
print("factor_source   :", ds.get("factor_source"))
print("scoring_degraded:", ds.get("scoring_degraded"))
print("fallback_reason :", str(ds.get("fallback_reason"))[:220])
print("ai_scoring_error:", str(ds.get("ai_scoring_error"))[:220])
print("error           :", str(ds.get("error"))[:220])
print("studio versions :", (r.get("studio") or {}).get("versions"))

q = requests.get(f"{SB}/rest/v1/sweep_queue",
                 params={"select": "status,attempts,error,duration_ms,claimed_at,updated_at",
                         "opp_id": f"eq.{OID[:15]}"},
                 headers=SH, verify=False, timeout=(10, 60)).json()
print("\nsweep_queue     :", json.dumps(q, default=str)[:400])

runs = requests.get(f"{SB}/rest/v1/deal_trigger_runs",
                    params={"select": "status,error,duration_ms,created_at,source,model",
                            "opp_id": f"eq.{OID}", "order": "created_at.desc", "limit": "4"},
                    headers=SH, verify=False, timeout=(10, 60)).json()
print("\nrecent runs:")
for x in (runs or []):
    print(f"  {str(x.get('created_at'))[11:19]} {x.get('status'):>10} "
          f"{(str(round((x.get('duration_ms') or 0)/60000,1))+'m'):>7} src={x.get('source')} "
          f"err={str(x.get('error'))[:110]}")
