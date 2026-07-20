"""After the revert deploy (34744d8) lands: force-cycle the worker onto the NEW image via
boto3, then restore Bandhan + NORTHPORT and VERIFY deal_scores are actually present (not the
absent-scores bug d05f8b4 caused). Uses boto3 verify=False (the CLI hangs behind Zscaler).
"""
import warnings, time, datetime
warnings.filterwarnings("ignore")
import boto3, botocore.config, requests, urllib3
urllib3.disable_warnings()
import sys
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
DEALS = [("Bandhan", "006P700000H55TV"), ("NORTHPORT", "006P700000QFJwD")]


def ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")


def latest_worker_rev():
    a = ecs.list_task_definitions(familyPrefix="mase-worker", sort="DESC", maxResults=1)["taskDefinitionArns"]
    return int(a[0].split(":")[-1]) if a else 0


def running_revs():
    t = ecs.list_tasks(cluster="mase-cluster", serviceName="mase-worker", desiredStatus="RUNNING").get("taskArns", [])
    if not t:
        return []
    return [x["taskDefinitionArn"].split(":")[-1] for x in ecs.describe_tasks(cluster="mase-cluster", tasks=t)["tasks"]]


def scores(oid):
    r = requests.get(f"{SB}/rest/v1/deal_records",
                     params={"select": "updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio",
                             "opp_id": f"eq.{oid}"}, headers=SH, verify=False, timeout=(10, 60)).json()
    if not r:
        return None
    r = r[0]
    ds = r.get("scores") or {}
    hl = ds.get("headline") or {}
    sv = (r.get("studio") or {}).get("versions") or {}
    return {"upd": r.get("updated_at"), "present": bool(ds), "win": hl.get("win_position"),
            "mom": hl.get("deal_momentum"), "src": ds.get("factor_source"), "eng": sv.get("win")}


# 1) wait for the deploy to register a worker task-def NEWER than 247 (revert = 248+)
print(f"[{ts()}] waiting for revert deploy to register mase-worker:>=248 ...", flush=True)
t0 = time.time()
target = 0
while time.time() - t0 < 1500:
    try:
        target = latest_worker_rev()
        if target >= 248:
            print(f"[{ts()}] deploy registered mase-worker:{target}", flush=True)
            break
    except Exception as e:
        print(f"[{ts()}] ecs poll err {type(e).__name__}", flush=True)
    time.sleep(30)
if target < 248:
    print(f"[{ts()}] deploy did not register a new worker rev in 25m — check CI", flush=True)
    sys.exit(1)

# 2) force-cycle the worker onto the latest, wait for steady state
print(f"[{ts()}] force-cycling worker onto mase-worker:{target} ...", flush=True)
ecs.update_service(cluster="mase-cluster", service="mase-worker",
                   taskDefinition=f"mase-worker:{target}", forceNewDeployment=True)
t0 = time.time()
while time.time() - t0 < 300:
    time.sleep=__import__("time").sleep
    time.sleep(20)
    s = ecs.describe_services(cluster="mase-cluster", services=["mase-worker"])["services"][0]
    revs = running_revs()
    print(f"[{ts()}]  running={s['runningCount']} rollout={s['deployments'][0].get('rolloutState')} revs={revs}", flush=True)
    if s["deployments"][0].get("rolloutState") == "COMPLETED" and revs and all(x == str(target) for x in revs):
        print(f"[{ts()}] worker on mase-worker:{target}, steady", flush=True)
        break

# 3) restore + verify each deal has deal_scores PRESENT and non-null
print(f"[{ts()}] enqueue + verify {len(DEALS)} deals", flush=True)
base = {}
for lbl, oid in DEALS:
    base[oid] = (scores(oid) or {}).get("upd")
    res = ((requests.post(f"{API}/api/deal-engine/sweep/trigger", headers=AH,
                          json={"opp_id": oid, "source": "manual"}, verify=False, timeout=60).json() or {})
           .get("results") or {}).get(oid)
    print(f"[{ts()}] enqueue {lbl}: {res}", flush=True)
    time.sleep(1)
done = {}
t0 = time.time()
while len(done) < len(DEALS) and time.time() - t0 < 2400:
    time.sleep(45)
    for lbl, oid in DEALS:
        if oid in done:
            continue
        a = scores(oid)
        if a and a["upd"] != base[oid] and a["present"]:
            ok = a["win"] is not None and a["src"] == "ai" and str(a["eng"]) == "10.8"
            print(f"[{ts()}] {'OK ' if ok else 'CHK'} {lbl:10} scores_present={a['present']} win={a['win']} "
                  f"mom={a['mom']} src={a['src']} v{a['eng']}", flush=True)
            done[oid] = a
    if len(done) < len(DEALS):
        print(f"[{ts()}]  ... {len(done)}/{len(DEALS)} restored ({int(time.time()-t0)//60}m)", flush=True)
print(f"[{ts()}] RESTORE COMPLETE: {len(done)}/{len(DEALS)}", flush=True)
print("POST-REVERT-DONE", flush=True)
