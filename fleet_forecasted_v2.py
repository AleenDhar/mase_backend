"""Finish the forecasted fleet at HIGH concurrency. Green is already scaled to 8
tasks; we proved (live) the org is at SCALE tier (10M ITPM / 2M OTPM / 20K RPM) and
using ~0%, so Anthropic is not the constraint — ECS compute is. Reads PENDING deals
from the DB (skips any already v10.8+ai, so no double-firing the in-flight/done ones),
fires them in-process at conc 24, verifies, retries timeouts at conc 6, scales back.

Usage: python fleet_forecasted_v2.py [--conc 24]
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

CONC = int(sys.argv[sys.argv.index("--conc") + 1]) if "--conc" in sys.argv else 24

SEL = ("updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio,"
       "acct:record->hard->account_name")


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
            "eng": sv.get("win"), "acct": r.get("acct")}


def is_done(a):
    return bool(a and a["present"] and a["win"] is not None and a["src"] == "ai" and str(a["eng"]) == "10.8")


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


def run_one(oid, poll_s=2200):
    base = rec(oid)
    lbl = str((base or {}).get("acct") or oid)[:24]
    bu = (base or {}).get("upd")
    try:
        requests.post(f"{API}/api/deal-engine/sweep/{oid}", headers=AH, json={},
                      verify=False, timeout=(10, 1400))
    except Exception:
        pass
    t0 = time.time()
    while time.time() - t0 < poll_s:
        time.sleep(25)
        a = rec(oid)
        if a and a["upd"] != bu and a["present"] and a["win"] is not None:
            ok = is_done(a)
            print(f"[{ts()}] {'OK ' if ok else 'CHK'} {lbl:24} win={a['win']} mom={a['mom']} "
                  f"v{a['eng']} src={a['src']} [{a['read']}]", flush=True)
            RESULTS[oid] = {"lbl": lbl, "a": a, "ok": ok}
            return
    print(f"[{ts()}] TIMEOUT {lbl}", flush=True)
    RESULTS[oid] = {"lbl": lbl, "a": rec(oid), "ok": False}


IDS = json.load(open("cc_work/_forecasted.json"))["ids"]
print(f"[{ts()}] forecasted v2: checking which of {len(IDS)} are already done …", flush=True)
pending, done = [], 0
for oid in IDS:
    if is_done(rec(oid)):
        done += 1
    else:
        pending.append(oid)
print(f"[{ts()}] already v10.8+ai: {done} | PENDING: {len(pending)} | conc={CONC}", flush=True)

COLOUR = live_colour()
s = ecs.describe_services(cluster="mase-cluster", services=[COLOUR])["services"][0]
print(f"[{ts()}] live colour {COLOUR} running={s['runningCount']} tasks", flush=True)

if pending:
    print(f"[{ts()}] firing {len(pending)} at conc {CONC} …", flush=True)
    with ThreadPoolExecutor(max_workers=CONC) as ex:
        list(ex.map(run_one, pending))
    retry = [oid for oid, r in RESULTS.items() if not r["ok"]]
    if retry:
        print(f"\n[{ts()}] retrying {len(retry)} not-OK at conc 6 …", flush=True)
        with ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(lambda o: run_one(o, poll_s=2600), retry))

print(f"\n[{ts()}] scaling {COLOUR} back to 2", flush=True)
try:
    scale(COLOUR, 2)
except Exception as e:  # noqa: BLE001
    print(f"[{ts()}] scale-back error (do manually): {e}", flush=True)

ok = sum(1 for r in RESULTS.values() if r["ok"])
chk = [r for r in RESULTS.values() if not r["ok"]]
print(f"\n===== v2 DONE: swept {len(RESULTS)} this pass | {ok} on v10.8+ai | book done={done+ok}/{len(IDS)} =====")
if chk:
    print("NOT v10.8+ai: " + ", ".join(r["lbl"] for r in chk))
json.dump(RESULTS, open("cc_work/_forecasted_v2_results.json", "w"), default=str)
print("FORECASTED-V2-DONE")
