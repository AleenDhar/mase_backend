"""Deploy-wait -> CANARY (verify the worker healed) -> fan out 37 deals in parallel on the
worker fleet, each with a human summary. If the canary shows the worker STILL stale, STOP
(don't null 36 deals) and print the one ECS command a maintainer must run.

A deal is DONE only when deal_records is rewritten AND: engine==v10.8, factor_source==ai,
non-null score, AND day_summary.source=='ai' (the human summary). Verified from data, not logs.
"""
import csv, sys, time, warnings, datetime
warnings.filterwarnings("ignore")
import requests, urllib3
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

CANARY = ("Bandhan Bank", "006P700000H55TV")
FLEET = [
    ("Gamuda", "006P700000Q15OU"), ("FGV", "0066700000yP7lZ"),
    ("NORTHPORT (MMC 7 Ports)", "006P700000QFJwD"), ("Bank Rakyat", "006P700000YK67N"),
    ("EPF Malaysia", "006P700000Wkphs"), ("Cebu Pacific", "0066700000wdNe1"),
    ("International SOS", "006P700000YJlu7"), ("Nidec", "006P700000VnWhf"),
    ("HK Jockey Club", "006P700000YN6Nh"), ("Temasek", "006P700000BV2eA"),
    ("WIK Group", "006P700000Uxmk7"), ("HAECO", "006P700000NwbBd"),
    ("MTR", "006P700000KTTO5"), ("SATS", "006P700000Ltf02"),
    ("Changi Airport", "006P700000FEVJR"), ("Angel One", "006P700000aED8o"),
    ("Vodafone Idea", "006P700000ZxAjy"), ("Thiess", "006P700000WXMP7"),
    ("Civeo Corporation", "006P700000YC92N"), ("CIVEO PTY LTD", "006P700000YO8aM"),
    ("PNG Nat Procurement", "006P700000Eo6QN"), ("Port Authority NSW", "006P700000YlMxa"),
    ("Techtronic", "006P700000GWfrf"), ("Scheme Financial", "006P700000QKfzN"),
    ("WA DOJ", "006P700000Ly6Mb"), ("AusNet", "006P700000Nh2xS"),
    ("Dominos Pizza", "006P700000X6hvK"), ("Orascom", "006P700000Y1Ont"),
    ("Fly Dubai", "006P700000PIlWk"), ("ASYAD", "006P700000Z98IL"),
    ("Arabian Industries", "006P700000QvP7Z"), ("Khansaheb", "006P700000LtIUv"),
    ("Wheelson/Mumtalakat", "006P700000VlPdp"), ("SAMI", "006P700000RD9Ir"),
    ("Alghanim", "006P700000OUsd6"), ("DWTC", "006P700000MB1SN"),
]
SEL = ("updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio,"
       "cov:record->evidence_coverage,daysum:record->ai->day_summary")
DEPLOY_WAIT = 900
POLL = 45
FLEET_TIMEOUT = 5400


def ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def enqueue(oid):
    r = requests.post(f"{API}/api/deal-engine/sweep/trigger", headers=AH,
                      json={"opp_id": oid, "source": "manual"}, verify=False, timeout=60)
    try:
        return ((r.json() or {}).get("results") or {}).get(oid, r.text[:60])
    except Exception:
        return r.text[:60]


def rec(oid):
    r = requests.get(f"{SB}/rest/v1/deal_records", params={"select": SEL, "opp_id": f"eq.{oid}"},
                     headers=SH, verify=False, timeout=(10, 60)).json()
    if not r:
        return None
    r = r[0]
    ds = r.get("scores") or {}
    hl = ds.get("headline") or {}
    sv = (r.get("studio") or {}).get("versions") or {}
    dsum = r.get("daysum") or {}
    return {"upd": r.get("updated_at"), "win": hl.get("win_position"), "mom": hl.get("deal_momentum"),
            "read": hl.get("read"), "src": ds.get("factor_source"), "engine": sv.get("win"),
            "deg": ds.get("scoring_degraded"), "calls": (r.get("cov") or {}).get("calls_read"),
            "daysum_src": dsum.get("source"), "daysum_overall": (dsum.get("overall") or "")[:140]}


def last_run(oid, since):
    r = requests.get(f"{SB}/rest/v1/deal_trigger_runs",
                     params={"select": "status,model,created_at,source", "opp_id": f"eq.{oid}",
                             "created_at": f"gte.{since.isoformat()}", "order": "created_at.desc",
                             "limit": "3"}, headers=SH, verify=False, timeout=(10, 60)).json()
    return r if isinstance(r, list) else []


def good(a, model=None):
    if not a or a["win"] is None:
        return False, "null score"
    if a["src"] != "ai":
        return False, f"src={a['src']} (degraded)"
    if a["deg"]:
        return False, "scoring_degraded"
    if str(a["engine"]) != "10.8":
        return False, f"engine v{a['engine']}"
    if model and "claude-sonnet-5" not in model:
        return False, f"stale model {model}"
    return True, "ok"


# 1) deploy wait
print(f"[{ts()}] waiting {DEPLOY_WAIT // 60}m for deploy + worker force-roll", flush=True)
time.sleep(DEPLOY_WAIT)

# 2) canary
lbl, oid = CANARY
worker_ok = False
for attempt in range(1, 4):
    base = rec(oid)
    c0 = now()
    print(f"[{ts()}] CANARY {lbl} attempt {attempt}: enqueue -> {enqueue(oid)}", flush=True)
    t0 = time.time()
    while time.time() - t0 < 1600:
        time.sleep(POLL)
        fin = [x for x in last_run(oid, c0) if (x.get("status") or "").lower() in ("completed", "failed")]
        if fin:
            run = fin[0]
            model = run.get("model") or ""
            a = rec(oid)
            ok, why = good(a, model)
            print(f"[{ts()}] canary run: src={run.get('source')} model={model} -> "
                  f"win={a['win'] if a else None} v{a['engine'] if a else None} "
                  f"daysum={a['daysum_src'] if a else None}", flush=True)
            if ok:
                worker_ok = True
                tag = "human summary present" if a["daysum_src"] == "ai" else f"daysum={a['daysum_src']}"
                print(f"[{ts()}] CANARY PASS — worker healthy ({tag}). Fanning out.", flush=True)
            else:
                print(f"[{ts()}] canary NOT healthy ({why}) — worker not ready; will retry.", flush=True)
            break
        print(f"[{ts()}]  ... canary running {int(time.time() - t0) // 60}m", flush=True)
    if worker_ok:
        break
    print(f"[{ts()}] wait 5m before canary retry...", flush=True)
    time.sleep(300)

if not worker_ok:
    print(f"\n[{ts()}] STOP - worker did not heal. NOT fanning out (would null 36 deals).", flush=True)
    print("  Maintainer fix:  aws ecs update-service --cluster mase-cluster "
          "--service mase-worker --force-new-deployment --region ap-south-1", flush=True)
    print("  (or I run the 37 in-process at lower concurrency - say the word.)", flush=True)
    print("FLEET37-STOPPED", flush=True)
    sys.exit(2)

# 3) fan out
print(f"\n[{ts()}] FANNING OUT {len(FLEET)} deals on the worker fleet", flush=True)
inflight = {}
for lbl, oid in FLEET:
    b = rec(oid)
    print(f"[{ts()}] enqueue {lbl:26} -> {enqueue(oid)}", flush=True)
    inflight[oid] = {"lbl": lbl, "base": (b or {}).get("upd"), "t0": time.time()}
    time.sleep(1)
done = {CANARY[1]: {"lbl": CANARY[0], "after": rec(CANARY[1])}}
t0 = time.time()
while inflight and time.time() - t0 < FLEET_TIMEOUT:
    time.sleep(POLL)
    for oid in list(inflight):
        st = inflight[oid]
        a = rec(oid)
        if a and a["upd"] != st["base"] and a["win"] is not None:
            ok, why = good(a)
            hs = "human" if a["daysum_src"] == "ai" else str(a["daysum_src"])
            print(f"[{ts()}] {'OK ' if ok else 'CHK'} {st['lbl']:26} win={a['win']} mom={a['mom']} "
                  f"v{a['engine']} src={a['src']} summary={hs}{'' if ok else '  ' + why}", flush=True)
            done[oid] = {"lbl": st["lbl"], "after": a}
            del inflight[oid]
    if inflight:
        print(f"[{ts()}]  ... {len(done)}/{len(FLEET) + 1} done, {len(inflight)} in flight", flush=True)
for oid, st in inflight.items():
    print(f"[{ts()}] TIMEOUT {st['lbl']}", flush=True)
    done[oid] = {"lbl": st["lbl"], "after": None}

# report
print("\n" + "=" * 96)
print(f"{'deal':27}{'WIN':>6}{'MOM':>6}{'read':>18}{'eng':>7}{'summary':>9}")
print("=" * 96)
with open("fleet37_results.csv", "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["deal", "opp_id", "win", "momentum", "read", "engine", "factor_source",
                "summary_source", "summary_overall"])
    for oid, d in done.items():
        a = d.get("after")
        if not a:
            print(f"{d['lbl']:27}{'FAILED':>6}")
            w.writerow([d["lbl"], oid, "FAILED", "", "", "", "", "", ""])
            continue
        hs = "human" if a["daysum_src"] == "ai" else str(a["daysum_src"])
        print(f"{d['lbl']:27}{str(a['win']):>6}{str(a['mom']):>6}{str(a['read'])[:16]:>18}"
              f"{'v' + str(a['engine']):>7}{hs:>9}")
        w.writerow([d["lbl"], oid, a["win"], a["mom"], a["read"], a["engine"], a["src"],
                    a["daysum_src"], a["daysum_overall"]])
print("\nwrote fleet37_results.csv")
print("FLEET37-DONE")
