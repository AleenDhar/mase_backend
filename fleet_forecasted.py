"""Sweep ALL forecasted deals (Commit / Best Case / Upside Key Deal) IN-PROCESS on
Omnivision v10.8 — POST /sweep/{oid} -> analyze_one (the proven reliable path; the
worker/queue is idle under manual_only).

Robust vs fleet37_inproc: detects the LIVE ALB colour (never hardcodes blue, so a
mid-run deploy flip can't drain the tier we scaled), scales THAT colour up for
capacity, runs at a GENTLE concurrency (18 saturated the shared tier last time),
verifies each record is fresh + v10.8 + src=ai, retries timeouts once at conc 3,
then scales back.

Usage: python fleet_forecasted.py [--tasks 4] [--conc 6]
"""
import sys, time, json, warnings, datetime
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
elb = boto3.client("elbv2", region_name="ap-south-1", verify=False, config=BC)

TASKS = int(sys.argv[sys.argv.index("--tasks") + 1]) if "--tasks" in sys.argv else 4
CONC = int(sys.argv[sys.argv.index("--conc") + 1]) if "--conc" in sys.argv else 6

SEL = ("updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio,"
       "cov:record->ai->evidence_coverage,acct:record->hard->account_name,"
       "fc:record->hard->forecast_category,amt:record->hard->amount")


def ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")


def rec(oid):
    try:
        r = requests.get(f"{SB}/rest/v1/deal_records", params={"select": SEL, "opp_id": f"eq.{oid}"},
                         headers=SH, verify=False, timeout=(10, 60)).json()
    except Exception:
        return None
    if not r:
        return None
    r = r[0]
    ds = r.get("scores") or {}
    hl = ds.get("headline") or {}
    sv = (r.get("studio") or {}).get("versions") or {}
    return {"upd": r.get("updated_at"), "present": bool(ds), "win": hl.get("win_position"),
            "mom": hl.get("deal_momentum"), "read": hl.get("read"), "src": ds.get("factor_source"),
            "eng": sv.get("win"), "calls": (r.get("cov") or {}).get("calls_read"),
            "acct": r.get("acct"), "fc": r.get("fc")}


def live_colour():
    """Which api colour the ALB currently forwards to (weight 100)."""
    lbs = elb.describe_load_balancers()["LoadBalancers"]
    alb = next((l for l in lbs if "mase-alb" in l["LoadBalancerName"]), lbs[0])
    lst = elb.describe_listeners(LoadBalancerArn=alb["LoadBalancerArn"])["Listeners"]
    http = next((x for x in lst if x["Port"] == 80), lst[0])
    tgs = http["DefaultActions"][0].get("ForwardConfig", {}).get("TargetGroups", [])
    live_tg = max(tgs, key=lambda t: t.get("Weight", 0))["TargetGroupArn"] if tgs else None
    for svc in ("mase-api-blue", "mase-api-green"):
        s = ecs.describe_services(cluster="mase-cluster", services=[svc])["services"][0]
        for lb in s.get("loadBalancers", []):
            if lb.get("targetGroupArn") == live_tg:
                return svc
    return "mase-api-blue"


def scale(svc, n):
    ecs.update_service(cluster="mase-cluster", service=svc, desiredCount=n)
    t0 = time.time()
    while time.time() - t0 < 300:
        s = ecs.describe_services(cluster="mase-cluster", services=[svc])["services"][0]
        if s["runningCount"] >= n and s["deployments"][0].get("rolloutState") == "COMPLETED":
            return True
        time.sleep(15)
    return False


RESULTS = {}


def run_one(oid, poll_s=2400):
    base = rec(oid)
    lbl = (base or {}).get("acct") or oid
    lbl = str(lbl)[:24]
    bu = (base or {}).get("upd")
    try:
        requests.post(f"{API}/api/deal-engine/sweep/{oid}", headers=AH, json={},
                      verify=False, timeout=(10, 1400))
    except Exception:
        pass  # ALB idle timeout — sweep continues server-side; we poll the DB
    t0 = time.time()
    while time.time() - t0 < poll_s:
        time.sleep(30)
        a = rec(oid)
        if a and a["upd"] != bu and a["present"] and a["win"] is not None:
            ok = a["src"] == "ai" and str(a["eng"]) == "10.8"
            thin = " THIN" if (a["calls"] or 0) <= 2 else ""
            print(f"[{ts()}] {'OK ' if ok else 'CHK'} {lbl:24} win={a['win']} mom={a['mom']} "
                  f"v{a['eng']} src={a['src']} calls={a['calls']} [{a['read']}]{thin}", flush=True)
            RESULTS[oid] = {"lbl": lbl, "a": a, "ok": ok}
            return
    print(f"[{ts()}] TIMEOUT {lbl}", flush=True)
    RESULTS[oid] = {"lbl": lbl, "a": rec(oid), "ok": False}


IDS = json.load(open("cc_work/_forecasted.json"))["ids"]
print(f"[{ts()}] forecasted fleet: {len(IDS)} deals | conc={CONC} | scale target={TASKS} tasks", flush=True)

COLOUR = live_colour()
print(f"[{ts()}] LIVE colour = {COLOUR} — scaling it to {TASKS} for in-process parallelism", flush=True)
scale(COLOUR, TASKS)

print(f"[{ts()}] firing {len(IDS)} in-process, {CONC} concurrent", flush=True)
with ThreadPoolExecutor(max_workers=CONC) as ex:
    list(ex.map(run_one, IDS))

# Retry timeouts / non-v10.8 once, gently (conc 3).
retry = [oid for oid, r in RESULTS.items() if not r["ok"]]
if retry:
    print(f"\n[{ts()}] retrying {len(retry)} not-OK deals at conc 3 …", flush=True)
    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(lambda o: run_one(o, poll_s=2600), retry))

print(f"\n[{ts()}] scaling {COLOUR} back to 2", flush=True)
try:
    scale(COLOUR, 2)
except Exception as e:  # noqa: BLE001
    print(f"[{ts()}] scale-back error (do manually): {e}", flush=True)

ok = [r for r in RESULTS.values() if r["ok"]]
chk = [r for r in RESULTS.values() if not r["ok"]]
print(f"\n===== FORECASTED FLEET DONE: {len(ok)}/{len(IDS)} on v10.8+ai =====")
print(f"{'deal':26}{'WIN':>4}{'MOM':>5}{'eng':>7}{'src':>6}{'read':>16}")
for r in sorted(RESULTS.values(), key=lambda z: (0 if z["ok"] else 1, z["lbl"])):
    a = r["a"] or {}
    print(f"{r['lbl']:26}{str(a.get('win')):>4}{str(a.get('mom')):>5}{'v'+str(a.get('eng')):>7}"
          f"{str(a.get('src')):>6}{str(a.get('read'))[:14]:>16}")
if chk:
    print(f"\nNOT on v10.8+ai ({len(chk)}): " + ", ".join(r["lbl"] for r in chk))
json.dump(RESULTS, open("cc_work/_forecasted_results.json", "w"), default=str)
print("FORECASTED-FLEET-DONE")
