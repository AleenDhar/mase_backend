"""READ-ONLY: who/what is actively sweeping? No AWS, no writes.

1) deal_records rows updated in the last N minutes (fleet/active-run detection).
2) win-engine rows in scoring_instructions (is v10.8 locked? by whom, when?).
3) recent deal_trigger_runs rows (in-flight / just-finished sweeps + their source/model).
"""
import datetime, json, sys, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"
cfg = {}
for line in open(ENV, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
KEY = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Accept": "application/json"}
now = datetime.datetime.now(datetime.timezone.utc)


def get(path, **params):
    r = requests.get(f"{SB}/rest/v1/{path}", params=params, headers=SH, verify=False, timeout=(10, 60))
    return r.json() if r.status_code == 200 else {"__err": r.status_code, "__body": r.text[:300]}


# 1) recently-updated deal_records (last 60 min) — is a fleet actively writing?
since = (now - datetime.timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
recent = get("deal_records", select="opp_id,opp_name,updated_at,swept_at",
             updated_at=f"gte.{since}", order="updated_at.desc", limit="60")
print(f"=== deal_records updated in last 60 min (since {since}) ===")
if isinstance(recent, list):
    print(f"  {len(recent)} row(s) updated recently")
    for r in recent[:40]:
        print(f"    {str(r.get('updated_at'))[:19]}  {str(r.get('opp_name'))[:34]:34} {r.get('opp_id')}")
else:
    print("  ", recent)

# 2) win-engine rows: is v10.8 locked, and by whom?
print("\n=== scoring_instructions: win engine rows (version/locked/by/at) ===")
win = get("scoring_instructions", select="version,locked,locked_by,locked_at,kind,note,created_at",
          engine="eq.win", order="created_at.desc", limit="12")
if isinstance(win, list):
    for r in win:
        lk = "LOCKED" if r.get("locked") else "draft "
        print(f"    v{str(r.get('version')):6} {lk} by={str(r.get('locked_by'))[:26]:26} "
              f"at={str(r.get('locked_at'))[:19]}  note={str(r.get('note'))[:50]}")
else:
    print("  ", win)

# also the current active-locked win/mom the runtime would resolve
print("\n=== active locked engines (win/mom/extract/todo/sum/sweep) ===")
for eng in ("win", "mom", "extract", "todo", "sum", "sweep"):
    rows = get("scoring_instructions", select="version,locked,locked_at",
               engine=f"eq.{eng}", locked="eq.true", order="locked_at.desc", limit="1")
    if isinstance(rows, list) and rows:
        print(f"    {eng:8} -> v{rows[0].get('version')} (locked {str(rows[0].get('locked_at'))[:19]})")
    else:
        print(f"    {eng:8} -> {rows}")

# 3) recent trigger runs (what's in-flight + source/model)
print("\n=== deal_trigger_runs: 30 most recent (status column) ===")
runs = get("deal_trigger_runs", select="*", order="created_at.desc", limit="30")
if isinstance(runs, list) and runs:
    cols = list(runs[0].keys())
    print("  columns:", ", ".join(cols))
    print()
    for r in runs:
        status = r.get("status")
        ts = str(r.get("created_at") or r.get("started_at"))[:19]
        fin = str(r.get("finished_at") or r.get("completed_at") or "")[:19] or "…RUNNING?"
        src = r.get("source"); mdl = str(r.get("model") or "")[:22]
        opp = r.get("opp_id") or r.get("opportunity_id")
        err = r.get("error") or r.get("error_message")
        print(f"    {ts} -> {fin:19} {str(status):11} src={str(src):8} model={mdl:22} {opp} "
              f"{('ERR:' + str(err)[:44]) if err else ''}")
    inflight = [r for r in runs if str(r.get("status")).lower()
                in ("running", "claimed", "waiting", "in_progress", "pending", "queued")]
    print(f"\n  -> {len(inflight)} run(s) IN-FLIGHT")
    for r in inflight:
        print(f"       {str(r.get('status'))}  {r.get('opp_id') or r.get('opportunity_id')}  "
              f"created={str(r.get('created_at'))[:19]}")
else:
    print("  ", runs)
