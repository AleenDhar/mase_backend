"""Post-deploy validation: sweep the 6 (7 opp) reconciler test set on the LIVE colour, verify
freshness + reconciler-era engine versions, and capture the reconciler's effect (resolved
packets + retire_evidence) for eyeballing (Birmingham=no-false-fires, Bright Horizons=§07)."""
import sys, time, json, warnings, datetime
from concurrent.futures import ThreadPoolExecutor
warnings.filterwarnings("ignore")
import requests, urllib3, boto3, botocore.config
urllib3.disable_warnings()
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
cfg = {}
for _l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local", encoding="utf-8"):
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1); cfg[k.strip()] = v.strip().strip('"').strip("'")
API = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
AH = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}", "Content-Type": "application/json"}
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/"); K = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": K, "Authorization": f"Bearer {K}"}
BC = botocore.config.Config(connect_timeout=10, read_timeout=35, retries={"max_attempts": 3})
ecs = boto3.client("ecs", region_name="ap-south-1", verify=False, config=BC)
elb = boto3.client("elbv2", region_name="ap-south-1", verify=False, config=BC)

OPPS = [
    ("Bright Horizons P2",  "006P700000aElYc"),
    ("Bright Horizons 2025","006P700000JwvB3"),
    ("Austrian Post",       "006P700000J71MD"),
    ("Robert Bosch",        "006P700000PlMpu"),
    ("Publicis Groupe",     "006P700000Xl06R"),
    ("SARS",                "006P700000UZv8c"),
    ("Birmingham",          "006P700000X6W3q"),
]
SEL = ("updated_at,eng:record->ai->scoring_studio->versions->win,"
       "w:record->ai->deal_scores->headline->win_position,"
       "m:record->ai->deal_scores->headline->deal_momentum,"
       "rd:record->ai->deal_scores->headline->read,pkts:record->packets")
PER_TO = 2600

def ts(): return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
def live_colour():
    lbs = elb.describe_load_balancers()["LoadBalancers"]
    alb = next((l for l in lbs if "mase-alb" in l["LoadBalancerName"]), lbs[0])
    lst = elb.describe_listeners(LoadBalancerArn=alb["LoadBalancerArn"])["Listeners"]
    http = next((x for x in lst if x["Port"] == 80), lst[0])
    tgs = http["DefaultActions"][0].get("ForwardConfig", {}).get("TargetGroups", [])
    w = {t["TargetGroupArn"].split("/")[-2]: t.get("Weight", 0) for t in tgs}
    return "mase-api-green" if w.get("mase-green", 0) >= w.get("mase-blue", 0) else "mase-api-blue"
def rec(oid):
    r = requests.get(f"{SB}/rest/v1/deal_records", params={"select": SEL, "opp_id": f"eq.{oid}"},
                     headers=SH, verify=False, timeout=(10, 90)).json()
    return r[0] if r else {}
def scale(svc, n):
    ecs.update_service(cluster="mase-cluster", service=svc, desiredCount=n)
    t0 = time.time()
    while time.time() - t0 < 360:
        s = ecs.describe_services(cluster="mase-cluster", services=[svc])["services"][0]
        if s["runningCount"] >= n and s["deployments"][0].get("rolloutState") == "COMPLETED": return
        time.sleep(15)
def pkt_stats(pkts):
    pkts = pkts or []
    act = [p for p in pkts if str(p.get("status") or "active") == "active"]
    res = [p for p in pkts if p.get("status") == "resolved"]
    retired_now = [p for p in res if p.get("retire_evidence")]
    req = len([p for p in act if p.get("type") == "requirement"])
    com = len([p for p in act if p.get("type") == "commitment"])
    return len(pkts), len(act), len(res), retired_now, req, com

RES = {}
def run_one(lbl, oid):
    base = rec(oid); bu = base.get("updated_at")
    try: requests.post(f"{API}/api/deal-engine/sweep/{oid}", headers=AH, json={}, verify=False, timeout=(10, 1500))
    except Exception: pass
    t0 = time.time()
    while time.time() - t0 < PER_TO:
        time.sleep(30)
        try: a = rec(oid)
        except Exception: continue
        if a.get("updated_at") != bu and a.get("w") is not None:
            tot, act, res, retired, req, com = pkt_stats(a.get("pkts"))
            RES[oid] = {"lbl": lbl, "a": a, "retired": retired, "req": req, "com": com, "res": res, "act": act}
            print(f"[{ts()}] OK {lbl:20} win={a.get('w')} mom={a.get('m')} v{a.get('eng')} "
                  f"| pkts:{act}act/{res}res req={req} com={com} retired_now={len(retired)}", flush=True)
            for rp in retired[:4]:
                v = rp.get("value") if isinstance(rp.get("value"), dict) else {}
                txt = rp.get("subject") or v.get("value") or v.get("requirement") or v.get("deliverable") or "?"
                print(f"        RETIRED[{rp.get('type')}]: {str(txt)[:80]!r} <- evidence: {str(rp.get('retire_evidence'))[:110]!r}", flush=True)
            return
    RES[oid] = {"lbl": lbl, "a": rec(oid), "timeout": True}
    print(f"[{ts()}] TIMEOUT {lbl}", flush=True)

if __name__ == "__main__":
    LIVE = live_colour()
    print(f"[{ts()}] live={LIVE} | scaling to 6 for {len(OPPS)} validation opps", flush=True)
    scale(LIVE, 6)
    with ThreadPoolExecutor(max_workers=7) as ex:
        list(ex.map(lambda d: run_one(*d), OPPS))
    print(f"[{ts()}] scaling {LIVE} back to 2", flush=True)
    try: scale(LIVE, 2)
    except Exception as e: print(f"scaleback err {e}", flush=True)
    ok = sum(1 for r in RES.values() if not r.get("timeout"))
    print(f"\n{ok}/{len(OPPS)} swept. RECONCILER SUMMARY:", flush=True)
    for lbl, oid in OPPS:
        r = RES.get(oid, {})
        if r.get("timeout"): print(f"  {lbl:20} TIMEOUT"); continue
        print(f"  {lbl:20} v{r['a'].get('eng')} req={r['req']} com={r['com']} retired_now={len(r['retired'])}")
    print("VALIDATE-DONE")
