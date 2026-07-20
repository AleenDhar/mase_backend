"""Re-sweep all Formal Evaluation + Shortlisted active opps (161) on the DEPLOYED new
logic (rev 295: from-scratch + top-10 activity deep-read + prompts). Scales the LIVE api
colour, fires /sweep/{oid} at bounded concurrency, verifies v10.9, tracks progress,
scales back. Resumable: skips deals already on v10.9."""
import sys, time, json, warnings, datetime, csv
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

TASKS, CONC, PER_DEAL_TO = 26, 22, 2600
NEW_ENG = "10.9"
DEALS = [(d["acct"] or d["opp_id"], d["opp_id"], d["stage"])
         for d in json.load(open("cc_work/_combined_set.json", encoding="utf-8"))]
SEL = "updated_at,eng:record->ai->scoring_studio->versions->win,w:record->ai->deal_scores->headline->win_position,m:record->ai->deal_scores->headline->deal_momentum,rd:record->ai->deal_scores->headline->read"
done = 0
RES = {}


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
    r = requests.get(f"{SB}/rest/v1/deal_records", params={"select": SEL, "opp_id": f"eq.{oid}"},
                     headers=SH, verify=False, timeout=(10, 60)).json()
    return r[0] if r else {}


def scale(svc, n):
    ecs.update_service(cluster="mase-cluster", service=svc, desiredCount=n)
    t0 = time.time()
    while time.time() - t0 < 360:
        s = ecs.describe_services(cluster="mase-cluster", services=[svc])["services"][0]
        if s["runningCount"] >= n and s["deployments"][0].get("rolloutState") == "COMPLETED":
            return
        time.sleep(15)


def run_one(lbl, oid, stage):
    global done
    base = rec(oid)
    if str(base.get("eng")) == NEW_ENG:  # resume: already fresh
        done += 1
        RES[oid] = {"lbl": lbl, "ok": True, "skipped": True, "a": base}
        print(f"[{ts()}] SKIP {done}/{len(DEALS)} {lbl[:26]:26} already v{NEW_ENG}", flush=True)
        return
    bu = base.get("updated_at")
    try:
        requests.post(f"{API}/api/deal-engine/sweep/{oid}", headers=AH, json={}, verify=False, timeout=(10, 1500))
    except Exception:
        pass
    t0 = time.time()
    while time.time() - t0 < PER_DEAL_TO:
        time.sleep(30)
        try:
            a = rec(oid)
        except Exception:
            continue
        if a.get("updated_at") != bu and a.get("w") is not None:
            ok = str(a.get("eng")) == NEW_ENG
            done += 1
            RES[oid] = {"lbl": lbl, "ok": ok, "a": a}
            print(f"[{ts()}] {'OK ' if ok else 'CHK'} {done}/{len(DEALS)} {lbl[:26]:26} "
                  f"win={a.get('w')} mom={a.get('m')} v{a.get('eng')} [{stage[:12]}]", flush=True)
            return
    done += 1
    RES[oid] = {"lbl": lbl, "ok": False, "a": rec(oid)}
    print(f"[{ts()}] TIMEOUT {done}/{len(DEALS)} {lbl[:26]}", flush=True)


LIVE = live_colour()
print(f"[{ts()}] {len(DEALS)} deals | live api={LIVE} -> scaling to {TASKS}, conc {CONC}", flush=True)
scale(LIVE, TASKS)
print(f"[{ts()}] sweeping…", flush=True)
with ThreadPoolExecutor(max_workers=CONC) as ex:
    list(ex.map(lambda d: run_one(*d), DEALS))

print(f"[{ts()}] scaling {LIVE} back to 2", flush=True)
try:
    scale(LIVE, 2)
except Exception as e:
    print(f"[{ts()}] scale-back err {e}", flush=True)

ok = sum(1 for r in RES.values() if r["ok"])
skip = sum(1 for r in RES.values() if r.get("skipped"))
with open("fe_sl_results.csv", "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["deal", "opp_id", "ok", "win", "mom", "read", "engine"])
    for lbl, oid, stage in DEALS:
        r = RES.get(oid, {}); a = r.get("a") or {}
        w.writerow([lbl, oid, r.get("ok"), a.get("w"), a.get("m"), a.get("rd"), a.get("eng")])
print(f"\n{ok}/{len(DEALS)} on v{NEW_ENG} ({skip} were already fresh). "
      f"{len(DEALS)-ok} need a look. wrote fe_sl_results.csv")
print("FE-SL-FLEET-DONE")
