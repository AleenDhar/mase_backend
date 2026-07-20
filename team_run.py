"""MASE — run Omnivision deal analysis for a list of opportunities (PRODUCTION).

Enqueues each opportunity to the durable sweep_queue via the deployed API; the autoscaled
mase-worker fleet drains them in parallel and writes governed scores to deal_records (the
front-end reads that live). This script does NOT change how deals are scored — the scoring is
governed by the locked Omnivision engines in Supabase. It only RUNS sweeps and verifies quality.

Safety design (learned the hard way on 2026-07-09):
  * CANARY first — run ONE deal, confirm the worker produced a healthy governed score, and only
    then fan the rest out. This catches a stale/broken worker before it can null 20 deals.
  * Quality gate is version-AGNOSTIC — it adopts whatever engine the Studio currently serves
    (captured from the canary), and requires factor_source=="ai", a non-null score, a present
    engine version, and model=claude-sonnet-5. A "hybrid"/degraded/null result is a FAIL.
  * A deal is DONE only when deal_records is rewritten AND passes the gate. HTTP 202 is not "done".
  * Unhealthy result → re-enqueue ONCE, then escalate. Never ship a degraded score.
"""
import csv, sys, time, warnings, datetime
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ============================ CONFIG — FILL THIS IN ============================
# The 4 values live in the MASE frontend `.env.local` (ask a maintainer if you don't have it).
# Do NOT use the AWS CLI / load_secret() to fetch them — it hangs behind the corporate proxy.
# Either paste the 4 values here, OR set ENV_LOCAL_PATH to your frontend .env.local and leave
# these blank (the script will read them from that file).
CONFIG = {
    "API_BASE": "",       # DEAL_ENGINE_API_BASE      e.g. http://mase-alb-XXXX.elb.amazonaws.com
    "API_TOKEN": "",      # DEAL_ENGINE_TOKEN
    "SUPABASE_URL": "",   # NEXT_PUBLIC_SUPABASE_URL
    "SUPABASE_KEY": "",   # SUPABASE_SERVICE_ROLE_KEY
}
ENV_LOCAL_PATH = r""      # e.g. C:\Users\you\MASE\frontend\.env.local  (blank = use CONFIG above)

# The opportunities to run. Salesforce 15- or 18-char Ids. Label is just for your reading.
OPPS = [
    # ("Account name", "006P700000XXXXX"),
]
# ==============================================================================

if ENV_LOCAL_PATH:
    for _l in open(ENV_LOCAL_PATH, encoding="utf-8"):
        _l = _l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            k, v = _l.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "DEAL_ENGINE_API_BASE":
                CONFIG["API_BASE"] = CONFIG["API_BASE"] or v
            elif k == "DEAL_ENGINE_TOKEN":
                CONFIG["API_TOKEN"] = CONFIG["API_TOKEN"] or v
            elif k == "NEXT_PUBLIC_SUPABASE_URL":
                CONFIG["SUPABASE_URL"] = CONFIG["SUPABASE_URL"] or v
            elif k == "SUPABASE_SERVICE_ROLE_KEY":
                CONFIG["SUPABASE_KEY"] = CONFIG["SUPABASE_KEY"] or v

API = CONFIG["API_BASE"].rstrip("/")
AH = {"Authorization": f"Bearer {CONFIG['API_TOKEN']}", "Content-Type": "application/json"}
SB = CONFIG["SUPABASE_URL"].rstrip("/")
SH = {"apikey": CONFIG["SUPABASE_KEY"], "Authorization": f"Bearer {CONFIG['SUPABASE_KEY']}"}
assert API and CONFIG["API_TOKEN"] and SB and CONFIG["SUPABASE_KEY"], \
    "Fill CONFIG (or set ENV_LOCAL_PATH) before running."
assert OPPS, "Add at least one opportunity to OPPS."

VERIFY = False            # corp TLS interception; the proxy already inspects traffic
POLL = 40                 # seconds between checks
CANARY_TIMEOUT = 1500     # 25 min for the single canary (cold worker start is normal)
FLEET_TIMEOUT = 5400      # 90 min for the whole fan-out
PER_DEAL_SLOW = 1800      # warn if a single deal exceeds 30 min
SEL = ("updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio,"
       "cov:record->evidence_coverage")


def ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def enqueue(oid):
    r = requests.post(f"{API}/api/deal-engine/sweep/trigger", headers=AH,
                      json={"opp_id": oid, "source": "manual"}, verify=VERIFY, timeout=60)
    try:
        return ((r.json() or {}).get("results") or {}).get(oid, r.text[:80])
    except Exception:
        return f"HTTP {r.status_code} {r.text[:60]}"


def rec(oid):
    r = requests.get(f"{SB}/rest/v1/deal_records", params={"select": SEL, "opp_id": f"eq.{oid}"},
                     headers=SH, verify=VERIFY, timeout=(10, 60)).json()
    if not r:
        return None
    r = r[0]
    ds = r.get("scores") or {}
    hl = ds.get("headline") or {}
    sv = (r.get("studio") or {}).get("versions") or {}
    return {"upd": r.get("updated_at"), "win": hl.get("win_position"), "mom": hl.get("deal_momentum"),
            "commit": hl.get("customer_commitment"), "risk": hl.get("deal_risk"), "read": hl.get("read"),
            "src": ds.get("factor_source"), "engine": sv.get("win"), "degraded": ds.get("scoring_degraded"),
            "calls": (r.get("cov") or {}).get("calls_read"), "reasons": ds.get("ai_reasons") or {}}


def last_run(oid, since):
    r = requests.get(f"{SB}/rest/v1/deal_trigger_runs",
                     params={"select": "status,model,error,created_at", "opp_id": f"eq.{oid}",
                             "created_at": f"gte.{since.isoformat()}", "order": "created_at.desc",
                             "limit": "3"}, headers=SH, verify=VERIFY, timeout=(10, 60)).json()
    return r if isinstance(r, list) else []


def healthy(a, model, expected_engine=None):
    """Version-agnostic quality gate. A degraded/hybrid/null/stale-model result FAILS."""
    if not a or a["win"] is None:
        return False, "null score (deal_scores empty)"
    if a["src"] != "ai":
        return False, f"factor_source={a['src']} (degraded keyword fallback, not governed)"
    if a["degraded"]:
        return False, "scoring_degraded flag set"
    if not a["engine"]:
        return False, "no Studio engine version stamped"
    if model and "claude-sonnet-5" not in model:
        return False, f"wrong model {model} (stale worker image)"
    if expected_engine and str(a["engine"]) != str(expected_engine):
        return False, f"engine v{a['engine']} != expected v{expected_engine}"
    return True, "ok"


# --------------------------------------------------------------- 1. CANARY
c_lbl, c_oid = OPPS[0]
print(f"[{ts()}] CANARY — {c_lbl} — verifying a worker before fanning out {len(OPPS)-1} more")
c_before = rec(c_oid)
c_start = utcnow()
print(f"[{ts()}] enqueue {c_lbl}: {enqueue(c_oid)}   (was win={(c_before or {}).get('win')} "
      f"mom={(c_before or {}).get('mom')})")
expected_engine = None
canary_after = None
t0 = time.time()
while time.time() - t0 < CANARY_TIMEOUT:
    time.sleep(POLL)
    finished = [x for x in last_run(c_oid, c_start)
                if (x.get("status") or "").lower() in ("completed", "failed")]
    if finished:
        run = finished[0]
        a = rec(c_oid)
        ok, why = healthy(a, run.get("model"))
        canary_after = a
        if ok:
            expected_engine = a["engine"]
            print(f"[{ts()}] CANARY PASS — win={a['win']} mom={a['mom']} engine=v{a['engine']} "
                  f"model={run.get('model')} calls={a['calls']}")
        else:
            print(f"[{ts()}] CANARY FAIL — {why} | run status={run.get('status')} "
                  f"model={run.get('model')} err={str(run.get('error'))[:120]}")
        break
    print(f"[{ts()}]  … canary running ({int(time.time()-t0)//60}m)")

if expected_engine is None:
    print(f"\n[{ts()}] STOP. The canary did not produce a healthy governed score, so the fleet "
          f"is NOT released. Do not force it. Likely causes: a stale mase-worker image (run "
          f"logs model=claude-sonnet-4-5), a degraded scorer (factor_source=hybrid), or the "
          f"worker fleet not scaling. Escalate to a MASE maintainer with the line above.")
    sys.exit(2)

# --------------------------------------------------------------- 2. FAN OUT
print(f"\n[{ts()}] Fanning out {len(OPPS)-1} deals in parallel (worker fleet). Expected engine "
      f"v{expected_engine}.")
inflight = {}
for lbl, oid in OPPS[1:]:
    b = rec(oid)
    print(f"[{ts()}] enqueue {lbl}: {enqueue(oid)}")
    inflight[oid] = {"lbl": lbl, "before": b, "t0": time.time(), "retried": False}
    time.sleep(1)

done = {c_oid: {"lbl": c_lbl, "before": c_before, "after": canary_after, "ok": True}}
t0 = time.time()
while inflight and time.time() - t0 < FLEET_TIMEOUT:
    time.sleep(POLL)
    for oid in list(inflight):
        st = inflight[oid]
        a = rec(oid)
        fresh = a and a["upd"] != (st["before"] or {}).get("upd") and a["win"] is not None
        if fresh:
            runs = last_run(oid, utcnow() - datetime.timedelta(hours=3))
            model = runs[0].get("model") if runs else ""
            ok, why = healthy(a, model, expected_engine)
            thin = "  THIN(calls<=2)" if (a["calls"] or 0) <= 2 else ""
            print(f"[{ts()}] {'OK  ' if ok else 'CHECK'} {st['lbl']:22} win={a['win']} mom={a['mom']} "
                  f"read={a['read']!r} v{a['engine']} calls={a['calls']}{thin}"
                  f"{'' if ok else '  -> ' + why}")
            if not ok and not st["retried"]:
                st["retried"] = True
                st["t0"] = time.time()
                st["before"] = a
                print(f"[{ts()}]   re-enqueue {st['lbl']} (unhealthy, 1 retry): {enqueue(oid)}")
                continue
            done[oid] = {"lbl": st["lbl"], "before": st["before"], "after": a, "ok": ok}
            del inflight[oid]
        elif time.time() - st["t0"] > PER_DEAL_SLOW:
            print(f"[{ts()}] SLOW {st['lbl']} > {PER_DEAL_SLOW//60}m in flight. Leaving it — the "
                  f"worker will finish or reclaim it. If MANY are stuck, the fleet may not be "
                  f"scaling; escalate to a maintainer.")
            st["t0"] = time.time()
    left = len(inflight)
    if left:
        print(f"[{ts()}]  progress: {len(done)}/{len(OPPS)} done, {left} in flight")

for oid, st in inflight.items():
    print(f"[{ts()}] TIMEOUT {st['lbl']} — did not finish in {FLEET_TIMEOUT//60}m")
    done[oid] = {"lbl": st["lbl"], "before": st["before"], "after": None, "ok": False}

# --------------------------------------------------------------- 3. REPORT
print("\n" + "=" * 100)
print(f"{'deal':24}{'WIN':>8}{'MOM':>8}{'COM':>6}{'RSK':>6}  {'read':<16}{'eng':>6}{'calls':>6}  flag")
print("=" * 100)
n_ok = n_thin = n_bad = 0
for r in sorted(done.values(), key=lambda x: (not x.get("ok"), x["lbl"])):
    a = r.get("after")
    if not a:
        n_bad += 1
        print(f"{r['lbl']:24}{'FAILED — re-run or escalate':>40}"); continue
    thin = (a["calls"] or 0) <= 2
    flag = "OK" if r.get("ok") and not thin else ("THIN-COVERAGE" if r.get("ok") else "NEEDS REVIEW")
    if r.get("ok") and not thin:
        n_ok += 1
    elif thin:
        n_thin += 1
    else:
        n_bad += 1
    print(f"{r['lbl']:24}{str(a['win']):>8}{str(a['mom']):>8}{str(a['commit']):>6}{str(a['risk']):>6}  "
          f"{str(a['read'])[:15]:<16}{'v'+str(a['engine']):>6}{str(a['calls']):>6}  {flag}")
print("-" * 100)
print(f"clean={n_ok}  thin-coverage={n_thin}  needs-review/failed={n_bad}  of {len(done)}")

with open("team_run_results.csv", "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["deal", "opp_id", "win", "momentum", "commitment", "risk", "read", "engine",
                "factor_source", "calls_read", "flag", "win_reasons", "momentum_reasons",
                "commitment_reasons", "risk_reasons"])
    for oid, r in done.items():
        a = r.get("after")
        if not a:
            w.writerow([r["lbl"], oid, "FAILED"] + [""] * 12); continue
        rs = a.get("reasons") or {}

        def j(k):
            return " || ".join(f"[{x.get('tone')}] {x.get('text')}" for x in (rs.get(k) or []))
        flag = "OK" if r.get("ok") and (a["calls"] or 0) > 2 else (
            "THIN" if r.get("ok") else "NEEDS_REVIEW")
        w.writerow([r["lbl"], oid, a["win"], a["mom"], a["commit"], a["risk"], a["read"],
                    a["engine"], a["src"], a["calls"], flag, j("win_position"),
                    j("deal_momentum"), j("customer_commitment"), j("deal_risk")])
print("wrote team_run_results.csv — review THIN and NEEDS_REVIEW rows before trusting them.")
print("TEAM-RUN-DONE")
