"""Controlled re-run of the 5 deals that timed out without a fresh v10.8 score.
IN-PROCESS (POST /sweep/{oid}), concurrency 3 (gentle on the shared API tier), verify each
gets deal_scores present + v10.8 + src=ai. Scales mase-api-blue back to 2 at the end.
"""
import sys, time, warnings, datetime
from concurrent.futures import ThreadPoolExecutor
warnings.filterwarnings("ignore")
import requests, urllib3, boto3, botocore.config
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

cfg = {}
for _l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local", encoding="utf-8"):
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
API = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
AH = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}", "Content-Type": "application/json"}
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
K = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": K, "Authorization": f"Bearer {K}"}
BC = botocore.config.Config(connect_timeout=10, read_timeout=35, retries={"max_attempts": 3})
ecs = boto3.client("ecs", region_name="ap-south-1", verify=False, config=BC)

DEALS = [
    ("Thiess", "006P700000WXMP7"), ("Changi Airport", "006P700000FEVJR"),
    ("Bank Rakyat", "006P700000YK67N"), ("FGV", "0066700000yP7lZ"),
    ("NORTHPORT", "006P700000QFJwD"),
]
CONC = 3
SEL = ("updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio,"
       "cov:record->evidence_coverage")


def ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")


def rec(oid):
    r = requests.get(f"{SB}/rest/v1/deal_records", params={"select": SEL, "opp_id": f"eq.{oid}"},
                     headers=SH, verify=False, timeout=(10, 60)).json()
    if not r:
        return None
    r = r[0]
    ds = r.get("scores") or {}
    hl = ds.get("headline") or {}
    sv = (r.get("studio") or {}).get("versions") or {}
    return {"upd": r.get("updated_at"), "present": bool(ds), "win": hl.get("win_position"),
            "mom": hl.get("deal_momentum"), "read": hl.get("read"), "src": ds.get("factor_source"),
            "eng": sv.get("win"), "calls": (r.get("cov") or {}).get("calls_read")}


RESULTS = {}


def run_one(lbl, oid):
    base = rec(oid)
    bu = (base or {}).get("upd")
    print(f"[{ts()}] -> {lbl}", flush=True)
    try:
        requests.post(f"{API}/api/deal-engine/sweep/{oid}", headers=AH, json={},
                      verify=False, timeout=(10, 1400))
    except Exception:
        pass
    t0 = time.time()
    while time.time() - t0 < 2400:
        time.sleep(30)
        a = rec(oid)
        if a and a["upd"] != bu and a["present"] and a["win"] is not None:
            ok = a["src"] == "ai" and str(a["eng"]) == "10.8"
            thin = " THIN" if (a["calls"] or 0) <= 2 else ""
            lost = " LOST" if a["read"] == "Lost" else ""
            print(f"[{ts()}] {'OK ' if ok else 'CHK'} {lbl:16} win={a['win']} mom={a['mom']} "
                  f"v{a['eng']} src={a['src']} calls={a['calls']}{thin}{lost}", flush=True)
            RESULTS[oid] = {"lbl": lbl, "a": a, "ok": ok}
            return
    print(f"[{ts()}] TIMEOUT {lbl}", flush=True)
    RESULTS[oid] = {"lbl": lbl, "a": rec(oid), "ok": False}


print(f"[{ts()}] re-running {len(DEALS)} deals in-process, {CONC} concurrent", flush=True)
with ThreadPoolExecutor(max_workers=CONC) as ex:
    list(ex.map(lambda d: run_one(*d), DEALS))

print(f"[{ts()}] scaling mase-api-blue back to 2", flush=True)
try:
    ecs.update_service(cluster="mase-cluster", service="mase-api-blue", desiredCount=2)
except Exception as e:
    print(f"[{ts()}] scale-back err {e}", flush=True)

print("\n" + "=" * 80)
for lbl, oid in DEALS:
    r = RESULTS.get(oid)
    a = (r or {}).get("a")
    if not a or not a.get("present"):
        print(f"{lbl:16} FAILED")
        continue
    print(f"{lbl:16} win={a['win']} mom={a['mom']} v{a['eng']} src={a['src']} calls={a['calls']}")
print("RERUN5-DONE")
