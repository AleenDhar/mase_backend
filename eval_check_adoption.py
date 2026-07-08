"""EVAL helper — (a) diff mom v10.2 (Sam's lock) vs v10.1 seed, (b) find records
already stamped with ai.scoring_studio (proof the worker injects), read-only."""
import difflib, json, sys, datetime
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
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
BASE = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
H = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}", "Content-Type": "application/json"}

print("now utc:", datetime.datetime.now(datetime.timezone.utc).isoformat())

# ---- (a) diff v10.1 -> v10.2
v101 = requests.get(f"{BASE}/api/deal-engine/scoring-studio/mom/version/10.1", headers=H,
                    verify=False, timeout=60).json().get("content") or ""
v102 = open("eval_original_mom_locked.txt", encoding="utf-8").read()
print(f"v10.1 {len(v101)} chars | v10.2 {len(v102)} chars")
diff = list(difflib.unified_diff(v101.splitlines(), v102.splitlines(),
                                 "mom v10.1", "mom v10.2", lineterm="", n=1))
print("\n".join(diff[:80]) if diff else "IDENTICAL content")

# ---- (b) records already stamped with scoring_studio provenance
sec = load_secret(); sb = sec["SUPABASE_URL"].rstrip("/")
key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
SH = {"apikey": key, "Authorization": f"Bearer {key}"}
rows = requests.get(f"{sb}/rest/v1/deal_records",
                    params={"select": "opp_id,account_name,opp_name,updated_at,record->ai->scoring_studio",
                            "active": "eq.true",
                            "record->ai->scoring_studio": "not.is.null",
                            "order": "updated_at.desc", "limit": "12"},
                    headers=SH, verify=VERIFY, timeout=120).json()
if isinstance(rows, dict):
    print("stamp query error:", json.dumps(rows)[:300])
else:
    print(f"\nrecords carrying ai.scoring_studio stamp: {len(rows)} (latest 12)")
    for r in rows:
        st = r.get("scoring_studio") or {}
        print(f"  {r['updated_at'][:19]}  {r['account_name'][:34]:34s}  versions={st.get('versions')}")
