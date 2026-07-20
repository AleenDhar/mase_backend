"""Run the EMEA list (Benelux / DACH / Nordics) IN-PROCESS. NO changes to code/prompts — just
sweeps. Scales mase-api-blue to 6, fires in-process at concurrency 8 (gentle, avoids the tail
contention that timed out the 18-wide run), verifies each has deal_scores present + v10.8 +
src=ai, flags THIN/LOST, then scales the API back to 2.
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

TASKS, CONC = 6, 8
DEALS = [
    ("Etex Group", "006P700000UGPE5"), ("Moore", "006P700000YV6To"), ("Nutreco", "006P700000YK7xp"),
    ("Ferrero", "006P700000aB9Pq"), ("Vestacy", "006P700000ZfllF"), ("PV Group", "006P700000Qjugc"),
    ("Hager Group", "006P700000QOnIL"), ("EVN AG", "006P700000YVZyo"), ("Evonik", "006P700000a0pZS"),
    ("Erste Group", "006P700000UOMxN"), ("Deutsche Telekom", "006P700000ZdMkT"),
    ("Kromberg & Schubert", "006P700000a0fGB"), ("Lapp Gruppe", "006P700000W1Rer"),
    ("Austrian Post", "006P700000J71MD"), ("Robert Bosch", "006P700000PlMpu"),
    ("Ahlstrom", "006P700000Wot0T"), ("ASSA ABLOY", "006P700000aAKSD"),
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
    ecs.update_service(cluster="mase-cluster", service="mase-api-blue", desiredCount=n)
    t0 = time.time()
    while time.time() - t0 < 300:
        s = ecs.describe_services(cluster="mase-cluster", services=["mase-api-blue"])["services"][0]
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
    while time.time() - t0 < 2600:
        time.sleep(30)
        a = rec(oid)
        if a and a["upd"] != bu and a["present"] and a["win"] is not None:
            ok = a["src"] == "ai" and str(a["eng"]) == "10.8"
            flag = " THIN" if (a["calls"] or 0) <= 2 else ""
            if a["read"] == "Lost":
                flag += " LOST"
            print(f"[{ts()}] {'OK ' if ok else 'CHK'} {lbl:20} win={a['win']} mom={a['mom']} "
                  f"v{a['eng']} src={a['src']} calls={a['calls']}{flag}", flush=True)
            RESULTS[oid] = {"lbl": lbl, "a": a, "ok": ok}
            return
    print(f"[{ts()}] TIMEOUT {lbl}", flush=True)
    RESULTS[oid] = {"lbl": lbl, "a": rec(oid), "ok": False}


print(f"[{ts()}] scaling mase-api-blue -> {TASKS}", flush=True)
scale(TASKS)
print(f"[{ts()}] firing {len(DEALS)} EMEA deals in-process, {CONC} concurrent", flush=True)
with ThreadPoolExecutor(max_workers=CONC) as ex:
    list(ex.map(lambda d: run_one(*d), DEALS))

print(f"[{ts()}] scaling mase-api-blue back to 2", flush=True)
try:
    scale(2)
except Exception as e:
    print(f"[{ts()}] scale-back err {e}", flush=True)

ok = sum(1 for r in RESULTS.values() if r["ok"])
print("\n" + "=" * 90)
print(f"{'deal':20}{'WIN':>6}{'MOM':>6}{'read':>16}{'eng':>7}{'calls':>6}")
print("=" * 90)
with open("emea_results.csv", "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["deal", "opp_id", "win", "momentum", "read", "engine", "factor_source", "calls_read", "ok"])
    for lbl, oid in DEALS:
        r = RESULTS.get(oid)
        a = (r or {}).get("a")
        if not a or not a.get("present"):
            print(f"{lbl:20}{'FAILED':>6}")
            w.writerow([lbl, oid, "FAILED", "", "", "", "", "", False])
            continue
        print(f"{lbl:20}{str(a['win']):>6}{str(a['mom']):>6}{str(a['read'])[:14]:>16}"
              f"{'v' + str(a['eng']):>7}{str(a['calls']):>6}")
        w.writerow([lbl, oid, a["win"], a["mom"], a["read"], a["eng"], a["src"], a["calls"], r["ok"]])
print(f"\n{ok}/{len(DEALS)} governed OK. wrote emea_results.csv")
print("EMEA-FLEET-DONE")
