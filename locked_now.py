"""Read-only: what active_locked() resolves to RIGHT NOW, straight from Supabase."""
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
SKEY = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": SKEY, "Authorization": f"Bearer {SKEY}"}


def vkey(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (-1,)


rows = requests.get(f"{SB}/rest/v1/scoring_instructions",
                    params={"select": "engine,version,locked,locked_at,locked_by,note",
                            "locked": "is.true"},
                    headers=SH, verify=False, timeout=(10, 60)).json()
best = {}
for r in rows:
    e = r["engine"]
    if e not in best or vkey(r["version"]) > vkey(best[e]["version"]):
        best[e] = r
print("ACTIVE LOCKED (what active_locked() returns on the next sweep):\n")
for e in ("extract", "win", "mom", "todo", "sum", "sweep", "vendordict", "playbook"):
    r = best.get(e)
    if not r:
        print(f"  {e:11} NONE LOCKED  <-- engine would be SKIPPED"); continue
    star = "  <<< GOVERNS DEAL SCORES" if e in ("win", "mom") else ""
    print(f"  {e:11} v{r['version']:<6} locked_at={str(r.get('locked_at'))[:19]} "
          f"by={r.get('locked_by')}{star}")
