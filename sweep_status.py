"""Read-only live status of the 7 in-flight AWS sweeps.

Sources (CloudWatch is unreachable from this Zscaler box — AWS CLI hangs):
  1. GET /api/deal-engine/sweep/status   — the API's own in-process view
  2. deal_trigger_runs                   — one row per FINISHED run (status, error, duration)
  3. deal_records.updated_at             — the write that proves the row landed
"""
import sys, json, warnings, datetime
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
API = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
AH = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}"}
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
SKEY = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": SKEY, "Authorization": f"Bearer {SKEY}"}

DEALS = [("SAMI", "006P700000RD9Ir", "2026-07-09T07:00:37"),
         ("Allstate", "006P7000006uKrq", "2026-07-09T11:41:05"),
         ("Robert Bosch", "006P700000PlMpu", "2026-07-09T07:00:37"),
         ("NORTHPORT", "006P700000QFJwD", "2026-07-09T07:00:37"),
         ("Domino's Pizza", "006P700000X6hvK", "2026-07-09T07:00:37"),
         ("Greencore", "006P700000WeRX8", "2026-07-09T07:00:37"),
         ("SARS", "006P700000UZv8c", "2026-07-08T08:28:33")]

print("=" * 96)
print("1) API in-process view  — GET /api/deal-engine/sweep/status")
print("=" * 96)
try:
    r = requests.get(f"{API}/api/deal-engine/sweep/status", headers=AH, verify=False, timeout=(10, 40))
    st = r.json()
    keep = {k: st[k] for k in ("running", "sweep_in_progress", "in_flight", "active", "queue",
                               "sweep_queue_active", "records_failed_validation", "phase",
                               "processed", "total", "started_at") if k in st}
    print(json.dumps(keep or st, indent=2, default=str)[:1400])
except Exception as e:
    print(f"  [err] {type(e).__name__}: {e}")

print()
print("=" * 96)
print("2) deal_trigger_runs — rows written TODAY (a row appears only when a run FINISHES)")
print("=" * 96)
today = datetime.date.today().isoformat()
try:
    runs = requests.get(f"{SB}/rest/v1/deal_trigger_runs",
                        params={"select": "opp_id,account_name,source,status,error,duration_ms,"
                                          "model,total_tokens,cost_usd,created_at",
                                "created_at": f"gte.{today}",
                                "order": "created_at.desc", "limit": "40"},
                        headers=SH, verify=False, timeout=(10, 60)).json()
except Exception as e:
    runs = []
    print(f"  [err] {type(e).__name__}: {e}")
oids = {o for _, o, _ in DEALS}
mine = [x for x in runs if x.get("opp_id") in oids]
if not mine:
    print("  (no finished-run rows yet for our 7 — consistent with all still executing)")
for x in mine:
    err = (x.get("error") or "")[:70]
    dur = x.get("duration_ms")
    print(f"  {str(x.get('account_name'))[:22]:24} {x['status']:>10} "
          f"{(str(round(dur/60000,1))+'m') if dur else '-':>7} src={x.get('source')} "
          f"{str(x.get('created_at'))[:19]} {('ERR: ' + err) if err else ''}")

print()
print("=" * 96)
print("3) deal_records — has the row been rewritten yet?")
print("=" * 96)
SEL = ("updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio")
done = 0
for label, oid, base in DEALS:
    try:
        r = requests.get(f"{SB}/rest/v1/deal_records",
                         params={"select": SEL, "opp_id": f"eq.{oid}"},
                         headers=SH, verify=False, timeout=(10, 60)).json()
        if not r:
            print(f"  {label:15} NO ROW"); continue
        r = r[0]
        up = str(r.get("updated_at") or "")
        ds = r.get("scores") or {}
        hl = ds.get("headline") or {}
        sv = (r.get("studio") or {}).get("versions") or {}
        fresh = not up.startswith(base)
        if fresh:
            done += 1
        deg = ds.get("scoring_degraded")
        flag = "DONE " if fresh else "…run "
        print(f"  {flag} {label:15} upd={up[:19]} win={hl.get('win_position')} "
              f"mom={hl.get('deal_momentum')} src={ds.get('factor_source')} "
              f"winEng=v{sv.get('win')}{'  DEGRADED!' if deg else ''}")
    except Exception as e:
        print(f"  {label:15} [err] {type(e).__name__}")
print(f"\n  => {done}/7 rewritten, {7-done}/7 still executing on ECS")
