"""Sweep 5 newly-requested deals in-process on Omnivision v10.8. Scales green to 6
first (headroom alongside the finishing cleanup pass), fires at conc 5, polls each
for a fresh v10.8+ai record, retries timeouts once at conc 3. Self-healing: if the
concurrent cleanup run scales green down mid-flight and drops a sweep, the retry
recovers it. Leaves green scaling to a final reconcile (does not scale down here)."""
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
    ("Birmingham City Council_UK", "006P700000X6W3q"),
    ("Yondr Group Limited", "006P700000YjU3D"),
    ("E&Y_UK", "006P700000X0TlP"),
    ("Keurig Dr Pepper", "006P700000Oi7xi"),
    ("Pinsent Masons", "006P700000ZUCcH"),
]
SEL = ("updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio")


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
            "eng": sv.get("win")}


def scale(svc, n):
    ecs.update_service(cluster="mase-cluster", service=svc, desiredCount=n)
    t0 = time.time()
    while time.time() - t0 < 240:
        s = ecs.describe_services(cluster="mase-cluster", services=[svc])["services"][0]
        if s["runningCount"] >= n and s["deployments"][0].get("rolloutState") == "COMPLETED":
            return True
        time.sleep(12)
    return False


RESULTS = {}


def run_one(item, poll_s=2400):
    lbl, oid = item
    base = rec(oid)
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
            ok = a["src"] == "ai" and str(a["eng"]) == "10.8"
            print(f"[{ts()}] {'OK ' if ok else 'CHK'} {lbl:26} win={a['win']} mom={a['mom']} "
                  f"v{a['eng']} src={a['src']} [{a['read']}]", flush=True)
            RESULTS[oid] = {"lbl": lbl, "a": a, "ok": ok}
            return
    print(f"[{ts()}] TIMEOUT {lbl}", flush=True)
    RESULTS[oid] = {"lbl": lbl, "a": rec(oid), "ok": False}


print(f"[{ts()}] add5: scaling green -> 6 for headroom", flush=True)
scale("mase-api-green", 6)
print(f"[{ts()}] firing {len(DEALS)} at conc 5 …", flush=True)
with ThreadPoolExecutor(max_workers=5) as ex:
    list(ex.map(run_one, DEALS))
retry = [(r["lbl"], oid) for oid, r in RESULTS.items() if not r["ok"]]
if retry:
    print(f"\n[{ts()}] retrying {len(retry)} at conc 3 …", flush=True)
    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(lambda it: run_one(it, poll_s=2600), retry))

ok = sum(1 for r in RESULTS.values() if r["ok"])
print(f"\n===== ADD5 DONE: {ok}/{len(DEALS)} on v10.8+ai =====")
for lbl, oid in DEALS:
    a = (RESULTS.get(oid) or {}).get("a") or {}
    print(f"  {lbl:26} win={a.get('win')} mom={a.get('mom')} v{a.get('eng')} src={a.get('src')} [{a.get('read')}]")
print("ADD5-DONE")
