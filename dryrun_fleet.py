"""Studio-v2 DRY-RUN driver — QA then the forecasted fleet. NOTHING touches deal_records.

Mechanism: POST /api/deal-engine/sweep/{opp}/datalake-test — a detached, dry_run=True sweep
on the DEPLOYED Studio-v2 pipeline; the full would-be record lands in datalake.ab_test_results
(a QA table, not the app DB). This driver polls that table and stores every completed record
LOCALLY under dryrun_forecasted/ (+ a summary CSV), so the data ships to the frontend ONLY
when the user later says "push" (a separate, explicit upsert pass).

Usage:
  python dryrun_fleet.py qa                 # QA pass: Publicis + John Deere, with validation
  python dryrun_fleet.py fleet              # all ACTIVE forecast_critical deals, batched
  python dryrun_fleet.py fleet --limit 5    # first N only
  python dryrun_fleet.py collect            # (re)download results for already-started opps
"""
import sys, os, csv, json, time, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
from daily_summary.common import load_secret, load_datalake, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dryrun_forecasted")
os.makedirs(OUTDIR, exist_ok=True)
CSV_PATH = os.path.join(OUTDIR, "_summary.csv")
BATCH = int(os.environ.get("DRYRUN_BATCH", "4"))          # concurrent detached sweeps
POLL_S = 45
PER_OPP_TIMEOUT_S = 2100                                   # 35 min hard cap per opp

# --- endpoints/creds ---------------------------------------------------------
ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"
cfg = {}
for line in open(ENV, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
API = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
AH = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}", "Content-Type": "application/json"}

sec = load_secret()
SB = sec["SUPABASE_URL"].rstrip("/")
SKEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
SH = {"apikey": SKEY, "Authorization": f"Bearer {SKEY}"}
dl = load_datalake()
DL = dl["DATALAKE_URL"].rstrip("/")
DKEY = dl["DATALAKE_SERVICE_KEY"]
DH = {"apikey": DKEY, "Authorization": f"Bearer {DKEY}"}


def forecasted():
    rows = requests.get(f"{SB}/rest/v1/deal_records",
                        params={"select": "opp_id,account_name,opp_name,stage,forecast_category,amount,close_date,record",
                                "active": "eq.true", "forecast_critical": "eq.true",
                                "order": "amount.desc.nullslast", "limit": "200"},
                        headers=SH, verify=VERIFY, timeout=120).json()
    return rows


def start(opp_id, name="", account="", owner=""):
    for attempt in range(4):
        try:
            r = requests.post(f"{API}/api/deal-engine/sweep/{opp_id}/datalake-test", headers=AH,
                              json={"name": name, "account": account, "owner": owner},
                              verify=False, timeout=60)
            return r.status_code, (r.json() if r.text else {})
        except requests.RequestException as e:
            print(f"  [start-retry {attempt+1}/4] {opp_id}: {type(e).__name__}")
            time.sleep(15 * (attempt + 1))
    return 599, {"error": "start failed after retries"}


def poll(opp_id):
    for attempt in range(3):
        try:
            r = requests.get(f"{DL}/rest/v1/ab_test_results",
                             params={"opp_id": f"eq.{opp_id}", "select": "opp_id,status,error,result"},
                             headers=DH, verify=VERIFY, timeout=60).json()
            return r[0] if isinstance(r, list) and r else None
        except requests.RequestException:
            time.sleep(10 * (attempt + 1))
    return None


_RETRYABLE = ("no salesforce/avoma tools loaded", "start failed after retries")


def reset_result(opp_id):
    """Clear a stale ab_test_results row so a fresh run's status is unambiguous."""
    requests.delete(f"{DL}/rest/v1/ab_test_results", params={"opp_id": f"eq.{opp_id}"},
                    headers=DH, verify=VERIFY, timeout=60)


def validate(opp_id, rec, prior_hl):
    """QA checks on a dry-run record from the Studio-v2 pipeline."""
    ai = (rec or {}).get("ai") or {}
    ds = ai.get("deal_scores") or {}
    hl = ds.get("headline") or {}
    sv = (ai.get("scoring_studio") or {}).get("versions") or {}
    checks = [
        (bool(rec), "record present"),
        (sv.get("sweep") == "10.0", f"provenance sweep=10.0 (got {sv.get('sweep')})"),
        (sv.get("extract") == "10.4", f"provenance extract=10.4 (got {sv.get('extract')})"),
        (sv.get("vendordict") == "1.0", f"provenance vendordict=1.0 (got {sv.get('vendordict')})"),
        (sv.get("playbook") == "1.0", f"provenance playbook=1.0 (got {sv.get('playbook')})"),
        (isinstance(ai.get("forecast_read"), dict), "ai.forecast_read emitted (v3 contract)"),
        (bool(str(((ai.get("north_star_verdict") or {}).get("verdict")) or "").strip()),
         "north_star_verdict present (adapter or carried)"),
        (hl.get("win_position") is not None, "win_position scored"),
        (hl.get("deal_momentum") is not None, "deal_momentum scored"),
        (ds.get("factor_source") == "ai", f"factor_source=ai (got {ds.get('factor_source')})"),
        (isinstance(ai.get("recommended_moves"), (list, dict)), "recommended_moves present"),
    ]
    print(f"  --- validation {opp_id} ---")
    fails = 0
    for ok, label in checks:
        print(("  OK   " if ok else "  FAIL ") + label)
        fails += (not ok)
    if prior_hl:
        print(f"  scores: win {prior_hl.get('win_position')} -> {hl.get('win_position')} | "
              f"mom {prior_hl.get('deal_momentum')} -> {hl.get('deal_momentum')}")
    return fails


QA_FAILS = {"n": 0}   # validation failures accumulated in qa mode (gates the fleet)


def run_batch(rows, validate_mode=False):
    """Start + poll a set of opps (batched), saving completed records locally."""
    todo = list(rows)
    results = []
    inflight = {}
    retries = {}   # opp_id -> retry count (transient failures re-queued, max 2)
    while todo or inflight:
        while todo and len(inflight) < BATCH:
            r = todo.pop(0)
            oid = r["opp_id"]
            try:
                reset_result(oid)
            except Exception:  # noqa: BLE001
                pass
            code, resp = start(oid, r.get("opp_name") or "", r.get("account_name") or "")
            print(f"[start] {r.get('account_name','')[:26]:26} {oid} -> {code}"
                  + (f" (retry {retries.get(oid)})" if retries.get(oid) else ""))
            inflight[oid] = {"row": r, "t0": time.time()}
            time.sleep(4)   # stagger starts — don't slam a freshly-flipped API
        time.sleep(POLL_S)
        for oid in list(inflight):
            st = poll(oid)
            age = time.time() - inflight[oid]["t0"]
            row = inflight[oid]["row"]
            if st and st.get("status") in ("completed", "failed"):
                res = st.get("result") or {}
                rec = res.get("record") if isinstance(res, dict) else None
                status = st.get("status")
                # TRANSIENT failure (API just flipped / MCP tools still loading): re-queue, max 2.
                _errtxt = str(st.get("error") or "")
                if status == "failed" and any(t in _errtxt for t in _RETRYABLE) and retries.get(oid, 0) < 2:
                    retries[oid] = retries.get(oid, 0) + 1
                    print(f"[requeue] {row.get('account_name','')[:26]:26} {oid} transient: {_errtxt[:60]}")
                    todo.append(row)
                    del inflight[oid]
                    continue
                if status == "completed" and rec:
                    with open(os.path.join(OUTDIR, f"{oid}.json"), "w", encoding="utf-8") as f:
                        json.dump(rec, f, indent=1, default=str)
                prior_hl = (((row.get("record") or {}).get("ai") or {}).get("deal_scores") or {}).get("headline") or {}
                ai = (rec or {}).get("ai") or {}
                hl = ((ai.get("deal_scores") or {}).get("headline") or {})
                results.append({
                    "opp_id": oid, "account": row.get("account_name"), "opp": row.get("opp_name"),
                    "stage": row.get("stage"), "forecast": row.get("forecast_category"),
                    "amount": row.get("amount"), "close": row.get("close_date"),
                    "status": status, "error": (st.get("error") or "")[:180],
                    "win_stored": prior_hl.get("win_position"), "win_dryrun": hl.get("win_position"),
                    "mom_stored": prior_hl.get("deal_momentum"), "mom_dryrun": hl.get("deal_momentum"),
                    "factor_source": ((ai.get("deal_scores") or {}).get("factor_source")),
                    "forecast_read_defensible": ((ai.get("forecast_read") or {}).get("defensible")
                                                 if isinstance(ai.get("forecast_read"), dict) else None),
                    "verdict": ((ai.get("north_star_verdict") or {}).get("verdict")),
                    "studio_versions": json.dumps((ai.get("scoring_studio") or {}).get("versions") or {}),
                    "calls_read": ((rec or {}).get("evidence_coverage") or {}).get("calls_read"),
                    "duration_s": int(age),
                })
                print(f"[done ] {row.get('account_name','')[:26]:26} {oid} status={status} "
                      f"win={hl.get('win_position')} mom={hl.get('deal_momentum')} src={(ai.get('deal_scores') or {}).get('factor_source')} "
                      f"({int(age)}s)" + (f" ERR={str(st.get('error'))[:70]}" if st.get("error") else ""))
                if validate_mode:
                    if status == "completed":
                        QA_FAILS["n"] += validate(oid, rec, prior_hl)
                    else:
                        QA_FAILS["n"] += 1
                del inflight[oid]
            elif age > PER_OPP_TIMEOUT_S:
                print(f"[TIMEOUT] {oid} after {int(age)}s — recording as timeout, moving on")
                results.append({"opp_id": oid, "account": row.get("account_name"), "opp": row.get("opp_name"),
                                "stage": row.get("stage"), "forecast": row.get("forecast_category"),
                                "amount": row.get("amount"), "close": row.get("close_date"),
                                "status": "timeout", "error": f"no result after {int(age)}s"})
                del inflight[oid]
        done_ct = len(results)
        print(f"  … in-flight {len(inflight)} | queued {len(todo)} | done {done_ct}")
    return results


def write_csv(results):
    if not results:
        return
    fields = ["opp_id", "account", "opp", "stage", "forecast", "amount", "close", "status", "error",
              "win_stored", "win_dryrun", "mom_stored", "mom_dryrun", "factor_source",
              "forecast_read_defensible", "verdict", "studio_versions", "calls_read", "duration_s"]
    exists = os.path.exists(CSV_PATH)
    seen = {}
    if exists:
        for r in csv.DictReader(open(CSV_PATH, encoding="utf-8-sig")):
            seen[r["opp_id"]] = r
    for r in results:
        seen[str(r["opp_id"])] = {k: r.get(k) for k in fields}
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in seen.values():
            w.writerow(r)
    print(f"\nCSV: {CSV_PATH} ({len(seen)} rows)")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "qa"
    rows = forecasted()
    print(f"forecasted (active, forecast_critical): {len(rows)} deals | mode={mode} | batch={BATCH}")
    if mode == "qa":
        qa_ids = {"006P700000Xl06R", "006P700000KHd9V"}   # Publicis, John Deere
        qa_rows = [r for r in rows if r["opp_id"] in qa_ids]
        if not qa_rows:  # they may not be forecast_critical — fetch directly
            qa_rows = requests.get(f"{SB}/rest/v1/deal_records",
                                   params={"select": "opp_id,account_name,opp_name,stage,forecast_category,amount,close_date,record",
                                           "opp_id": f"in.({','.join(qa_ids)})"},
                                   headers=SH, verify=VERIFY, timeout=60).json()
        res = run_batch(qa_rows, validate_mode=True)
        write_csv(res)
        print(f"\nQA VALIDATION FAILURES: {QA_FAILS['n']}")
        sys.exit(1 if QA_FAILS["n"] else 0)   # nonzero gates the chained fleet run
    elif mode == "fleet":
        lim = None
        if "--limit" in sys.argv:
            lim = int(sys.argv[sys.argv.index("--limit") + 1])
        done_already = {fn[:-5] for fn in os.listdir(OUTDIR) if fn.endswith(".json")}
        rows = [r for r in rows if r["opp_id"] not in done_already]
        if lim:
            rows = rows[:lim]
        print(f"fleet run: {len(rows)} to sweep (resume-safe; {len(done_already)} already local)")
        res = run_batch(rows)
        write_csv(res)
    elif mode == "collect":
        res = []
        for r in rows:
            st = poll(r["opp_id"])
            if st and st.get("status") == "completed" and (st.get("result") or {}).get("record"):
                with open(os.path.join(OUTDIR, f"{r['opp_id']}.json"), "w", encoding="utf-8") as f:
                    json.dump(st["result"]["record"], f, indent=1, default=str)
                print(f"collected {r['opp_id']}")
        print("done")
