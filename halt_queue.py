"""HALT: stop the stale mase-worker from claiming (and clobbering) more deals.

The worker is running an older task-definition/image: its runs report model
`claude-sonnet-4-5` (the api reports `claude-sonnet-5`) and, critically, it writes
deal_records rows with `ai.deal_scores = null` — wiping good governed scores.
Observed on NORTHPORT (27/8 -> null) and Robert Bosch (54/49 -> null).

worker.py's claim_one() only ever claims rows with status='waiting'. Flipping our
waiting rows to 'failed' makes them unclaimable, stopping the clobber loop. This
touches ONLY sweep_queue (an ops table). deal_records is never written here.

`working` rows are left alone — they are already claimed; corrupting their state
would be worse than letting them finish.
"""
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

OIDS = ["006P700000RD9Ir", "006P7000006uKrq", "006P700000PlMpu", "006P700000QFJwD",
        "006P700000X6hvK", "006P700000WeRX8", "006P700000UZv8c", "006P700000UGPE5"]
NOTE = "operator halt 2026-07-09: mase-worker on stale image writes null deal_scores"

IN = "(" + ",".join(OIDS) + ")"          # sweep_queue keys on the canonical 15-char id
rows = requests.get(f"{SB}/rest/v1/sweep_queue",
                    params={"select": "opp_id,account_name,status", "opp_id": f"in.{IN}",
                            "order": "updated_at.desc", "limit": "50"},
                    headers=SH, verify=False, timeout=(10, 60)).json()
if not isinstance(rows, list):
    print("sweep_queue read error:", rows); raise SystemExit(1)
mine = rows
print("before:")
for r in mine:
    print(f"  {str(r.get('account_name'))[:26]:28} {r['status']}")

waiting = [r for r in mine if r["status"] == "waiting"]
if not waiting:
    print("\nno `waiting` rows — nothing to halt")
else:
    for r in waiting:
        resp = requests.patch(f"{SB}/rest/v1/sweep_queue",
                              params={"opp_id": f"eq.{r['opp_id']}", "status": "eq.waiting"},
                              headers={**SH, "Content-Type": "application/json",
                                       "Prefer": "return=minimal"},
                              json={"status": "failed", "error": NOTE},
                              verify=False, timeout=(10, 60))
        print(f"\n  halted {str(r.get('account_name'))[:26]:28} -> HTTP {resp.status_code}")

rows2 = requests.get(f"{SB}/rest/v1/sweep_queue",
                     params={"select": "opp_id,account_name,status,error", "limit": "100"},
                     headers=SH, verify=False, timeout=(10, 60)).json()
print("\nafter:")
for r in [x for x in rows2 if x["opp_id"] in [o[:15] for o in OIDS] or x["opp_id"] in OIDS]:
    print(f"  {str(r.get('account_name'))[:26]:28} {r['status']:8} {str(r.get('error'))[:50]}")
