"""Re-run the remaining incomplete deals IN-PROCESS, network-RESILIENT (retries on DNS/conn
blips; a thread can never die and crash the batch). Reads cc_work/_rerun_remaining_named.json.
Scales mase-api-blue back to 2 at the end.
"""
import csv, sys, time, json, warnings, datetime
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

DEALS = json.load(open("cc_work/_rerun_remaining_named.json", encoding="utf-8"))
CONC = min(6, len(DEALS))
SEL = ("updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio,"
       "cov:record->evidence_coverage")


def ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")


def resilient_get(params, tries=5):
    for i in range(tries):
        try:
            return requests.get(f"{SB}/rest/v1/deal_records", params=params, headers=SH,
                                verify=False, timeout=(10, 60)).json()
        except Exception:
            time.sleep(5 * (i + 1))
    return None


def rec(oid):
    r = resilient_get({"select": SEL, "opp_id": f"eq.{oid}"})
    if not r:
        return None
    r = r[0]
    ds = r.get("scores") or {}
    hl = ds.get("headline") or {}
    sv = (r.get("studio") or {}).get("versions") or {}
    return {"upd": r.get("updated_at"), "present": bool(ds), "win": hl.get("win_position"),
            "mom": hl.get("deal_momentum"), "read": hl.get("read"), "src": ds.get("factor_source"),
            "eng": sv.get("win"), "calls": (r.get("cov") or {}).get("calls_read")}


def fire(oid, tries=4):
    for i in range(tries):
        try:
            requests.post(f"{API}/api/deal-engine/sweep/{oid}", headers=AH, json={},
                          verify=False, timeout=(10, 1400))
            return
        except Exception:
            time.sleep(5)  # ALB idle timeout is normal; only retry on connect failures


RESULTS = {}


def run_one(lbl, oid):
    try:
        base = rec(oid)
        bu = (base or {}).get("upd")
        fire(oid)
        t0 = time.time()
        while time.time() - t0 < 3000:
            time.sleep(30)
            a = rec(oid)
            if a and a["upd"] != bu and a["present"] and a["win"] is not None:
                ok = a["src"] == "ai" and str(a["eng"]) == "10.8"
                thin = " THIN" if (a["calls"] or 0) <= 2 else ""
                print(f"[{ts()}] {'OK ' if ok else 'CHK'} {lbl:20} win={a['win']} mom={a['mom']} "
                      f"v{a['eng']} src={a['src']} calls={a['calls']}{thin}", flush=True)
                RESULTS[oid] = {"lbl": lbl, "a": a, "ok": ok}
                return
        print(f"[{ts()}] TIMEOUT {lbl}", flush=True)
        RESULTS[oid] = {"lbl": lbl, "a": rec(oid), "ok": False}
    except Exception as e:  # a thread must NEVER crash the batch
        print(f"[{ts()}] ERR {lbl}: {type(e).__name__}: {str(e)[:100]}", flush=True)
        RESULTS[oid] = {"lbl": lbl, "a": None, "ok": False}


print(f"[{ts()}] resilient re-run of {len(DEALS)} deals, {CONC} concurrent (API already at 4)", flush=True)
with ThreadPoolExecutor(max_workers=CONC) as ex:
    list(ex.map(lambda d: run_one(d[0], d[1]), DEALS))

print(f"[{ts()}] scaling mase-api-blue back to 2", flush=True)
try:
    ecs.update_service(cluster="mase-cluster", service="mase-api-blue", desiredCount=2)
except Exception as e:
    print(f"[{ts()}] scale-back err {e}", flush=True)

ok = sum(1 for r in RESULTS.values() if r["ok"])
with open("rerun_remaining_results.csv", "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["deal", "opp_id", "win", "momentum", "read", "engine", "src", "calls", "ok"])
    for lbl, oid in DEALS:
        r = RESULTS.get(oid)
        a = (r or {}).get("a")
        if not a or not a.get("present"):
            w.writerow([lbl, oid, "FAILED", "", "", "", "", "", False])
            continue
        w.writerow([lbl, oid, a["win"], a["mom"], a["read"], a["eng"], a["src"], a["calls"], r["ok"]])
print(f"\n{ok}/{len(DEALS)} governed OK. wrote rerun_remaining_results.csv")
print("RERUN-REMAINING-DONE")
