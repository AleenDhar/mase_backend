"""Sweep the 9 untracked Qualified+ opps into the book (Omnivision v10.8, in-process).
Scales the LIVE api colour, fires POST /sweep/{oid} at concurrency 4, verifies each
lands a record with a v10.8 score, then scales back."""
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
elb = boto3.client("elbv2", region_name="ap-south-1", verify=False, config=BC)

CONC = 4
DEALS = [
    ("ALEC Engineering", "006P700000Q5eyjIAB"), ("JCDecaux", "006P700000XeYmPIAV"),
    ("Saudia Airlines", "006P700000aEeX8IAK"), ("GSK plc", "006P700000aZ93kIAC"),
    ("AAA Life Insurance", "006P700000aaPmMIAU"), ("SWBC", "006P700000afDRBIA2"),
    ("Bessemer Trust", "006P700000afMeeIAE"), ("Dar", "006P700000ajj9EIAQ"),
    ("Sadara", "006P700000akCUUIA2"),
]
SEL = ("updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio,"
       "cov:record->evidence_coverage")


def ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")


def live_colour():
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


def rec(oid):
    r = requests.get(f"{SB}/rest/v1/deal_records", params={"select": SEL, "opp_id": f"eq.{oid[:15]}*"},
                     headers=SH, verify=False, timeout=(10, 60))
    j = r.json()
    if not j:
        r = requests.get(f"{SB}/rest/v1/deal_records", params={"select": SEL, "opp_id": f"like.{oid[:15]}*"},
                         headers=SH, verify=False, timeout=(10, 60))
        j = r.json()
    if not j:
        return None
    x = j[0]
    ds = x.get("scores") or {}
    hl = ds.get("headline") or {}
    sv = (x.get("studio") or {}).get("versions") or {}
    return {"upd": x.get("updated_at"), "present": bool(ds), "win": hl.get("win_position"),
            "mom": hl.get("deal_momentum"), "read": hl.get("read"), "src": ds.get("factor_source"),
            "eng": sv.get("win"), "calls": (x.get("cov") or {}).get("calls_read")}


def scale(svc, n):
    ecs.update_service(cluster="mase-cluster", service=svc, desiredCount=n)
    t0 = time.time()
    while time.time() - t0 < 300:
        s = ecs.describe_services(cluster="mase-cluster", services=[svc])["services"][0]
        if s["runningCount"] >= n and s["deployments"][0].get("rolloutState") == "COMPLETED":
            return
        time.sleep(15)


RESULTS = {}


def run_one(lbl, oid):
    base = rec(oid)
    bu = (base or {}).get("upd")
    try:
        requests.post(f"{API}/api/deal-engine/sweep/{oid}", headers=AH, json={}, verify=False, timeout=(10, 1400))
    except Exception:
        pass
    t0 = time.time()
    while time.time() - t0 < 2400:
        time.sleep(30)
        try:
            a = rec(oid)
        except Exception:
            continue
        if a and a["present"] and a["win"] is not None and a["upd"] != bu:
            ok = a["src"] == "ai" and str(a["eng"]) == "10.8"
            print(f"[{ts()}] {'OK ' if ok else 'CHK'} {lbl:20} win={a['win']} mom={a['mom']} "
                  f"v{a['eng']} src={a['src']} calls={a['calls']}", flush=True)
            RESULTS[oid] = {"lbl": lbl, "a": a, "ok": ok}
            return
    print(f"[{ts()}] TIMEOUT {lbl}", flush=True)
    RESULTS[oid] = {"lbl": lbl, "a": rec(oid), "ok": False}


LIVE = live_colour()
print(f"[{ts()}] live api colour = {LIVE}; scaling -> 5", flush=True)
scale(LIVE, 5)
print(f"[{ts()}] sweeping {len(DEALS)} new opps in-process, {CONC} concurrent", flush=True)
with ThreadPoolExecutor(max_workers=CONC) as ex:
    list(ex.map(lambda d: run_one(*d), DEALS))

print(f"[{ts()}] scaling {LIVE} back to 2", flush=True)
try:
    scale(LIVE, 2)
except Exception as e:
    print(f"[{ts()}] scale-back err {e}", flush=True)

ok = sum(1 for r in RESULTS.values() if r["ok"])
print("\n" + "=" * 78)
for lbl, oid in DEALS:
    r = RESULTS.get(oid)
    a = (r or {}).get("a")
    if not a or not a.get("present"):
        print(f"{lbl:20} FAILED (no record)")
        continue
    print(f"{lbl:20} win={a['win']} mom={a['mom']} v{a['eng']} src={a['src']} calls={a['calls']}")
print(f"\n{ok}/{len(DEALS)} swept into the book. SWEEP9-DONE")
