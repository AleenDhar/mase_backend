"""Parallel fleet via the worker queue — deploy-wait, canary, then fan out 20-wide.

Flow (fully automated, safe):
  1. WAIT for the parallel-fleet deploy to serve traffic — a bogus-opp manual trigger returns
     'not_in_book' from the awaited enqueue_trigger (old in-process code returned 'accepted').
  2. CANARY — enqueue ONE deal (Robert Bosch, whose correct v10.8 answer ~54/58 we know from a
     clean api run). Wait for a worker to claim + complete it. PASS only if the run logs
     model=claude-sonnet-5 AND deal_records has NON-NULL v10.8 scores. A stale worker (the bug
     that nulled Bosch) logs claude-sonnet-4-5 / writes null → FAIL, and we STOP without touching
     the other deals.
  3. FAN OUT — on PASS, enqueue the remaining deals; the autoscaled worker fleet drains them in
     parallel. Skip any deal already freshly scored on v10.8 (fleet_108 in-process) to avoid
     double-spend. Watch sweep_queue + deal_records to completion; verify + report old->new.

Nothing here can write null: a stale worker is caught by the canary before the fleet is released.
"""
import csv, json, sys, time, threading, warnings, datetime
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"
cfg = {}
for _l in open(ENV, encoding="utf-8"):
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
API = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
AH = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}", "Content-Type": "application/json"}
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
K = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": K, "Authorization": f"Bearer {K}"}

CANARY = ("Robert Bosch", "006P700000PlMpu")
FLEET = [
    ("Temasek", "006P700000BV2eA"), ("Techtronic", "006P700000GWfrf"),
    ("SAMI", "006P700000RD9Ir"), ("Wheelson", "006P700000VlPdp"),
    ("Mair Group", "006P700000PtQGP"), ("MTR", "006P700000KTTO5"),
    ("Khansaheb", "006P700000LtIUv"), ("HAECO", "006P700000NwbBd"),
    ("Globe Telecom", "006P7000008hZHF"), ("Gamuda", "006P700000Q15OU"),
    ("Cebu Pacific", "0066700000wdNe1"), ("Bandhan Bank", "006P700000H55TV"),
    ("Arabian Industries", "006P700000QvP7Z"), ("ACEN", "006P700000DkWgX"),
    ("NORTHPORT", "006P700000QFJwD"), ("Etex Group", "006P700000UGPE5"),
]
PROBE = "006000000000000AAA"
EXPECT_ENGINE = "10.8"
FRESH_MIN = 25          # a record scored v10.8 within this many minutes counts as already done
POLL = 40
DEPLOY_WAIT_S = 1500
FLEET_WAIT_S = 3600
SEL = ("updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio,"
       "cov:record->evidence_coverage")


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
    upd = r.get("updated_at")
    fresh = False
    try:
        age = (now() - datetime.datetime.fromisoformat(str(upd).replace("Z", "+00:00"))).total_seconds()
        fresh = age < FRESH_MIN * 60
    except Exception:
        pass
    return {"upd": upd, "win": hl.get("win_position"), "mom": hl.get("deal_momentum"),
            "commit": hl.get("customer_commitment"), "risk": hl.get("deal_risk"),
            "read": hl.get("read"), "src": ds.get("factor_source"), "engine": sv.get("win"),
            "degraded": ds.get("scoring_degraded"),
            "calls_read": (r.get("cov") or {}).get("calls_read"),
            "fresh_v108": fresh and str(sv.get("win")) == EXPECT_ENGINE and ds.get("factor_source") == "ai",
            "reasons": ds.get("ai_reasons") or {}}


def qrows():
    oids = [CANARY[1]] + [o for _, o in FLEET]
    r = requests.get(f"{SB}/rest/v1/sweep_queue",
                     params={"select": "opp_id,account_name,status,attempts,error,claimed_at,updated_at",
                             "opp_id": f"in.({','.join(oids)})", "order": "updated_at.desc"},
                     headers=SH, verify=False, timeout=(10, 60)).json()
    return r if isinstance(r, list) else []


def run_model(oid, since):
    r = requests.get(f"{SB}/rest/v1/deal_trigger_runs",
                     params={"select": "status,model,duration_ms,error,created_at,source",
                             "opp_id": f"eq.{oid}", "created_at": f"gte.{since.isoformat()}",
                             "order": "created_at.desc", "limit": "3"},
                     headers=SH, verify=False, timeout=(10, 60)).json()
    return r if isinstance(r, list) else []


# ------------------------------------------------------------------ 1. deploy wait
print(f"[{ts()}] waiting for the parallel-fleet deploy to serve traffic…", flush=True)
t0 = time.time()
live = False
while time.time() - t0 < DEPLOY_WAIT_S:
    try:
        r = requests.post(f"{API}/api/deal-engine/sweep/trigger", headers=AH,
                          json={"opp_id": PROBE, "source": "manual"}, verify=False, timeout=40)
        res = ((r.json() or {}).get("results") or {}).get(PROBE)
        if res == "not_in_book":
            live = True
            print(f"[{ts()}] *** DEPLOY LIVE — manual triggers now enqueue (probe={res!r})", flush=True)
            break
        print(f"[{ts()}] not yet (probe={res!r})", flush=True)
    except Exception as e:
        print(f"[{ts()}] probe err {type(e).__name__}", flush=True)
    time.sleep(POLL)
if not live:
    print(f"[{ts()}] !!! deploy did not go live in {DEPLOY_WAIT_S//60}m — STOP", flush=True); sys.exit(1)


# ------------------------------------------------------------------ 2. canary
lbl, oid = CANARY
print(f"\n[{ts()}] CANARY — enqueue {lbl} and verify a worker writes real scores", flush=True)
before = rec(oid)
c_start = now()
print(f"[{ts()}] enqueue {lbl}: {enqueue(oid)}  (was win={before['win']} mom={before['mom']})", flush=True)
canary_ok = False
t0 = time.time()
while time.time() - t0 < 1500:
    time.sleep(POLL)
    runs = run_model(oid, c_start)
    finished = [x for x in runs if (x.get("status") or "").lower() in ("completed", "failed")]
    if finished:
        run = finished[0]
        model = run.get("model") or ""
        after = rec(oid)
        good_model = "claude-sonnet-5" in model
        non_null = after and after["win"] is not None and after["engine"] == EXPECT_ENGINE \
            and after["src"] == "ai" and not after["degraded"]
        print(f"[{ts()}] canary run: status={run.get('status')} model={model} "
              f"-> record win={after['win'] if after else None} mom={after['mom'] if after else None} "
              f"eng=v{after['engine'] if after else None} src={after['src'] if after else None}", flush=True)
        if good_model and non_null:
            canary_ok = True
            print(f"[{ts()}] ✅ CANARY PASS — worker is on the current image and writes real v10.8 "
                  f"scores. Releasing the fleet.", flush=True)
        else:
            print(f"[{ts()}] ❌ CANARY FAIL — model_ok={good_model} scores_ok={non_null}. "
                  f"Worker is unhealthy (stale image or null write). NOT releasing the fleet.", flush=True)
        break
    q = [x for x in qrows() if x.get("opp_id") in (oid, oid[:15])]
    st = q[0]["status"] if q else "?"
    print(f"[{ts()}]  … canary {lbl} queue={st} ({int(time.time()-t0)//60}m)", flush=True)

if not canary_ok:
    print(f"\n[{ts()}] CANARY DID NOT PASS — fleet NOT released. Investigate the worker image "
          f"before retrying (deploy.yml rolls it; a re-deploy may be needed).", flush=True)
    print("CANARY-FAILED", flush=True); sys.exit(2)


# ------------------------------------------------------------------ 3. fan out
print(f"\n[{ts()}] FAN OUT — enqueue {len(FLEET)} deals (skipping any already fresh on v10.8)", flush=True)
inflight, skipped = {}, []
for lbl, oid in FLEET:
    b = rec(oid)
    if b and b["fresh_v108"]:
        skipped.append((lbl, oid, b))
        print(f"[{ts()}]  skip {lbl:20} already v10.8 win={b['win']} mom={b['mom']} (fleet_108)", flush=True)
        continue
    res = enqueue(oid)
    inflight[oid] = {"lbl": lbl, "before": b, "t0": time.time()}
    print(f"[{ts()}]  enqueue {lbl:20} -> {res}", flush=True)
    time.sleep(1)

print(f"\n[{ts()}] {len(inflight)} enqueued, {len(skipped)} skipped. Watching the worker fleet drain…\n", flush=True)
done = {}
t0 = time.time()
while inflight and time.time() - t0 < FLEET_WAIT_S:
    time.sleep(POLL)
    qs = {x.get("opp_id"): x.get("status") for x in qrows()}
    working = sum(1 for v in qs.values() if v == "working")
    waiting = sum(1 for v in qs.values() if v == "waiting")
    for oid in list(inflight):
        st = inflight[oid]
        a = rec(oid)
        if a and a["upd"] != (st["before"] or {}).get("upd") and a["win"] is not None:
            ok = a["engine"] == EXPECT_ENGINE and a["src"] == "ai" and not a["degraded"]
            thin = "  ⚠THIN" if (a["calls_read"] or 0) <= 2 else ""
            print(f"[{ts()}] ✅ {st['lbl']:20} win={a['win']} mom={a['mom']} read={a['read']!r} "
                  f"eng=v{a['engine']} calls={a['calls_read']} {'OK' if ok else '⚠CHECK'}{thin} "
                  f"[was {(st['before'] or {}).get('win')}/{(st['before'] or {}).get('mom')}]", flush=True)
            done[oid] = {"lbl": st["lbl"], "before": st["before"], "after": a}
            del inflight[oid]
    print(f"[{ts()}]  fleet: {len(done)} done · {len(inflight)} left "
          f"(queue: {working} working, {waiting} waiting)", flush=True)

for oid, st in inflight.items():
    print(f"[{ts()}] ❌ {st['lbl']} did not finish in {FLEET_WAIT_S//60}m", flush=True)
    done[oid] = {"lbl": st["lbl"], "before": st["before"], "after": None}

# report + CSV (include the fleet_108-skipped deals so the table is the full set)
allrows = list(done.values()) + [{"lbl": l, "before": None, "after": b} for l, o, b in skipped] \
    + [{"lbl": CANARY[0], "before": before, "after": rec(CANARY[1])}]
print("\n" + "=" * 104)
print(f"{'deal':20}{'WIN':>10}{'MOM':>10}  {'read':<18}{'eng':>6}{'calls':>6}")
print("=" * 104)
for r in sorted(allrows, key=lambda x: x["lbl"]):
    a, b = r.get("after"), r.get("before") or {}
    if not a:
        print(f"{r['lbl']:20}{'FAILED':>10}"); continue
    dw = f"{a['win']}" + (f"({b.get('win')})" if b.get("win") not in (None, a['win']) else "")
    dm = f"{a['mom']}" + (f"({b.get('mom')})" if b.get("mom") not in (None, a['mom']) else "")
    print(f"{r['lbl']:20}{dw:>10}{dm:>10}  {str(a['read'])[:17]:<18}{'v'+str(a['engine']):>6}{str(a['calls_read']):>6}")

with open("parallel_fleet_results.csv", "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["deal", "win", "momentum", "commitment", "risk", "read", "engine",
                "factor_source", "calls_read", "prev_win", "prev_mom", "win_reasons", "momentum_reasons"])
    for r in sorted(allrows, key=lambda x: x["lbl"]):
        a, b = r.get("after"), r.get("before") or {}
        if not a:
            w.writerow([r["lbl"], "FAILED"] + [""] * 11); continue
        rs = a.get("reasons") or {}
        j = lambda k: " || ".join(f"[{x.get('tone')}] {x.get('text')}" for x in (rs.get(k) or []))
        w.writerow([r["lbl"], a["win"], a["mom"], a["commit"], a["risk"], a["read"], a["engine"],
                    a["src"], a["calls_read"], b.get("win"), b.get("mom"),
                    j("win_position"), j("deal_momentum")])
print("\nwrote parallel_fleet_results.csv")
print("PARALLEL-FLEET-DONE")
