"""Run the 37 deals IN-PROCESS (POST /sweep/{oid} -> analyze_one on the API, the proven path;
the worker/queue is idle under manual_only). Scales mase-api-blue up via boto3 for real
parallelism, fires with a concurrency cap sized to avoid OOM (~3-4 sweeps/task on 4 GB),
verifies each has deal_scores present + non-null + v10.8 + src=ai, then scales the API back.

Usage: python fleet37_inproc.py            # scale + run + verify + scale back
       python fleet37_inproc.py --tasks 6 --conc 18
"""
import csv, sys, time, warnings, datetime
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

TASKS = int(sys.argv[sys.argv.index("--tasks") + 1]) if "--tasks" in sys.argv else 4
CONC = int(sys.argv[sys.argv.index("--conc") + 1]) if "--conc" in sys.argv else 6
BLUE = "mase-api-blue"

DEALS = [
    ("Bandhan Bank", "006P700000H55TV"), ("Gamuda", "006P700000Q15OU"), ("FGV", "0066700000yP7lZ"),
    ("NORTHPORT", "006P700000QFJwD"), ("Bank Rakyat", "006P700000YK67N"), ("Cebu Pacific", "0066700000wdNe1"),
    ("Temasek", "006P700000BV2eA"), ("Changi Airport", "006P700000FEVJR"), ("Thiess", "006P700000WXMP7"),
    ("Scheme Financial", "006P700000QKfzN"), ("AusNet", "006P700000Nh2xS"), ("Orascom", "006P700000Y1Ont"),
    ("Fly Dubai", "006P700000PIlWk"), ("Wheelson/Mumtalakat", "006P700000VlPdp"),
]
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


def scale(n):
    ecs.update_service(cluster="mase-cluster", service=BLUE, desiredCount=n)
    t0 = time.time()
    while time.time() - t0 < 300:
        s = ecs.describe_services(cluster="mase-cluster", services=[BLUE])["services"][0]
        if s["runningCount"] >= n and s["deployments"][0].get("rolloutState") == "COMPLETED":
            return True
        time.sleep(15)
    return False


RESULTS = {}


def run_one(lbl, oid):
    base = rec(oid)
    bu = (base or {}).get("upd")
    try:
        requests.post(f"{API}/api/deal-engine/sweep/{oid}", headers=AH, json={},
                      verify=False, timeout=(10, 1400))
    except Exception:
        pass  # ALB idle timeout — sweep continues server-side
    t0 = time.time()
    while time.time() - t0 < 3000:
        time.sleep(30)
        a = rec(oid)
        if a and a["upd"] != bu and a["present"] and a["win"] is not None:
            ok = a["src"] == "ai" and str(a["eng"]) == "10.8"
            thin = " THIN" if (a["calls"] or 0) <= 2 else ""
            print(f"[{ts()}] {'OK ' if ok else 'CHK'} {lbl:22} win={a['win']} mom={a['mom']} "
                  f"v{a['eng']} src={a['src']} calls={a['calls']}{thin}", flush=True)
            RESULTS[oid] = {"lbl": lbl, "a": a, "ok": ok}
            return
    print(f"[{ts()}] TIMEOUT {lbl}", flush=True)
    RESULTS[oid] = {"lbl": lbl, "a": rec(oid), "ok": False}


print(f"[{ts()}] scaling {BLUE} -> {TASKS} tasks for in-process parallelism", flush=True)
scale(TASKS)
print(f"[{ts()}] firing {len(DEALS)} in-process, {CONC} concurrent", flush=True)
with ThreadPoolExecutor(max_workers=CONC) as ex:
    list(ex.map(lambda d: run_one(*d), DEALS))

print(f"[{ts()}] scaling {BLUE} back to 2", flush=True)
try:
    scale(2)
except Exception as e:
    print(f"[{ts()}] scale-back err {e}", flush=True)

ok = sum(1 for r in RESULTS.values() if r["ok"])
print("\n" + "=" * 92)
print(f"{'deal':22}{'WIN':>6}{'MOM':>6}{'read':>18}{'eng':>7}{'calls':>6}")
print("=" * 92)
with open("rerun14_results.csv", "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["deal", "opp_id", "win", "momentum", "read", "engine", "factor_source", "calls_read", "ok"])
    for lbl, oid in DEALS:
        r = RESULTS.get(oid)
        a = (r or {}).get("a")
        if not a or not a.get("present"):
            print(f"{lbl:22}{'FAILED':>6}")
            w.writerow([lbl, oid, "FAILED", "", "", "", "", "", False])
            continue
        print(f"{lbl:22}{str(a['win']):>6}{str(a['mom']):>6}{str(a['read'])[:16]:>18}"
              f"{'v' + str(a['eng']):>7}{str(a['calls']):>6}")
        w.writerow([lbl, oid, a["win"], a["mom"], a["read"], a["eng"], a["src"], a["calls"], r["ok"]])
print(f"\n{ok}/{len(DEALS)} governed OK. wrote rerun14_results.csv")
print("RERUN14-DONE")
