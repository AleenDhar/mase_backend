"""WATCHDOG for the 7 live AWS sweeps — poll, auto-diagnose, auto-re-trigger.

Rules (user-specified):
  * poll every 150s
  * a deal is only DONE if the row was rewritten AND factor_source=="ai" AND not scoring_degraded
    (a degraded row is a FAILURE — the deterministic keyword scorer, not Omnivision)
  * any run-log row with status!=completed / error -> re-trigger immediately
  * >30 min from trigger and not done -> surface, diagnose, re-trigger
  * max 2 re-triggers per deal, then hard-surface
  * when all 7 are done -> qa_live each + one combined summary

CloudWatch is unreachable from this box (AWS CLI hangs behind Zscaler), so "logs" =
deal_trigger_runs (status/error/duration written when a run finishes) + the API status endpoint.

qa_live.py imports daily_summary.common.load_secret which shells out to the AWS CLI; we inject
SUPABASE_URL/SF_* into its env so load_secret short-circuits to os.environ (no CLI, no hang).
"""
import json, os, re, subprocess, sys, time, warnings, datetime
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
SKEY = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": SKEY, "Authorization": f"Bearer {SKEY}"}

# qa_live env: makes load_secret() read os.environ instead of shelling out to the AWS CLI.
QA_ENV = {**os.environ, "SUPABASE_URL": SB, "SUPABASE_SERVICE_ROLE_KEY": SKEY,
          "SF_USERNAME": "unused-by-qa_live", "SF_PASSWORD": "unused-by-qa_live"}

# opp_id -> (label, baseline updated_at prefix at trigger time)
DEALS = [("SAMI", "006P700000RD9Ir", "2026-07-09T07:00:37"),
         ("Allstate", "006P7000006uKrq", "2026-07-09T11:41:05"),
         ("Robert Bosch", "006P700000PlMpu", "2026-07-09T07:00:37"),
         ("NORTHPORT", "006P700000QFJwD", "2026-07-09T07:00:37"),
         ("Domino's Pizza", "006P700000X6hvK", "2026-07-09T07:00:37"),
         ("Greencore", "006P700000WeRX8", "2026-07-09T07:00:37"),
         ("SARS", "006P700000UZv8c", "2026-07-08T08:28:33")]

# Anchor: SAMI's run row (created 14:11:11Z, duration 10.4m) => triggers fired ~14:00:45Z.
T0 = datetime.datetime.fromisoformat("2026-07-09T14:00:45+00:00")
POLL_S = 150
DEADLINE_S = 30 * 60
ESCALATE_S = 45 * 60       # still in-flight this long => wedged, say so loudly
MAX_RETRIES = 2
SEL = ("updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio,"
       "ceo:record->ai->ceo_intervention,cov:record->evidence_coverage")


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def ts():
    return now().strftime("%H:%M:%S")


def row(oid):
    r = requests.get(f"{SB}/rest/v1/deal_records", params={"select": SEL, "opp_id": f"eq.{oid}"},
                     headers=SH, verify=False, timeout=(10, 60)).json()
    return r[0] if isinstance(r, list) and r else None


def runlog(oid, since):
    """Finished-run rows for this opp since `since` (our 'logs')."""
    try:
        return requests.get(f"{SB}/rest/v1/deal_trigger_runs",
                            params={"select": "status,error,duration_ms,created_at,source,model",
                                    "opp_id": f"eq.{oid}", "created_at": f"gte.{since.isoformat()}",
                                    "order": "created_at.desc", "limit": "3"},
                            headers=SH, verify=False, timeout=(10, 60)).json() or []
    except Exception:
        return []


def trigger(oid):
    """-> (http_code, per-opp result). trigger_opp_async returns "already_running" when the
    in-flight claim (_trigger_inflight) is still held — the HTTP code is 202 REGARDLESS, so a
    naive status-code check would report a restart that never happened. Read the body."""
    r = requests.post(f"{API}/api/deal-engine/sweep/trigger", headers=AH,
                      json={"opp_id": oid, "source": "manual"}, verify=False, timeout=60)
    res = "?"
    try:
        res = ((r.json() or {}).get("results") or {}).get(oid, "?")
    except Exception:
        pass
    return r.status_code, res


def assess(oid, st):
    """-> (verdict, detail). verdict in DONE | RUNNING | FAIL"""
    r = row(oid)
    if not r:
        return "FAIL", "no deal_records row"
    up = str(r.get("updated_at") or "")
    ds = r.get("scores") or {}
    rewritten = not up.startswith(st["base"])

    logs = runlog(oid, st["t0"])
    bad = next((x for x in logs if (x.get("status") or "").lower() not in ("completed", "ok", "success")
                or x.get("error")), None)
    if bad:
        return "FAIL", (f"run-log status={bad.get('status')} "
                        f"error={str(bad.get('error'))[:140]}")

    if not rewritten:
        return "RUNNING", f"{int((now()-st['t0']).total_seconds())//60}m elapsed"

    if ds.get("scoring_degraded") or ds.get("factor_source") != "ai":
        return "FAIL", (f"DEGRADED — factor_source={ds.get('factor_source')} "
                        f"reason={str(ds.get('fallback_reason'))[:140]}")

    sv = (r.get("studio") or {}).get("versions") or {}
    if str(sv.get("win")) != "10.7":
        return "FAIL", f"wrong win engine v{sv.get('win')} (expected 10.7)"

    return "DONE", r


state = {oid: {"label": lbl, "base": base, "t0": T0, "retries": 0, "done": None}
         for lbl, oid, base in DEALS}
print(f"[{ts()}] watchdog up — 7 deals, poll {POLL_S}s, deadline {DEADLINE_S//60}m, "
      f"max {MAX_RETRIES} re-triggers/deal", flush=True)

while any(s["done"] is None for s in state.values()):
    for oid, st in state.items():
        if st["done"] is not None:
            continue
        try:
            verdict, detail = assess(oid, st)
        except Exception as e:
            print(f"[{ts()}] [poll-err] {st['label']}: {type(e).__name__}: {e}", flush=True)
            continue

        if verdict == "DONE":
            st["done"] = detail
            ds = detail.get("scores") or {}
            hl = ds.get("headline") or {}
            mins = int((now() - st["t0"]).total_seconds()) // 60
            print(f"[{ts()}] ### DONE {st['label']} ({mins}m) win={hl.get('win_position')} "
                  f"mom={hl.get('deal_momentum')} read={hl.get('read')!r} src=ai v10.7", flush=True)
            continue

        elapsed = (now() - st["t0"]).total_seconds()
        overdue = elapsed > DEADLINE_S
        if verdict == "FAIL" or overdue:
            why = detail if verdict == "FAIL" else f"TIMEOUT >{int(elapsed)//60}m ({detail})"
            print(f"[{ts()}] !!! PROBLEM {st['label']}: {why}", flush=True)
            for lg in runlog(oid, st["t0"]):
                print(f"[{ts()}]     runlog: status={lg.get('status')} "
                      f"dur={lg.get('duration_ms')} err={str(lg.get('error'))[:120]}", flush=True)
            if st["retries"] >= MAX_RETRIES:
                st["done"] = False
                print(f"[{ts()}] XXX GIVING UP {st['label']} after {MAX_RETRIES} re-triggers "
                      f"— needs a human", flush=True)
                continue
            code, res = trigger(oid)
            if res == "already_running":
                # The in-flight claim (_trigger_inflight, keyed on opp_id[:15]) is STILL held,
                # so the original sweep is alive on ECS and a restart is impossible until it
                # ends. Do NOT burn a retry, do NOT reset t0 — that would silently push the
                # deadline out and fake a restart. Surface it and keep watching.
                print(f"[{ts()}] ~~~ {st['label']} OVERDUE at {int(elapsed)//60}m but STILL "
                      f"EXECUTING on ECS (in-flight dedup -> 'already_running'). Cannot restart "
                      f"until it ends or dies; continuing to watch.", flush=True)
                if elapsed > ESCALATE_S:
                    print(f"[{ts()}] ^^^ ESCALATE {st['label']}: >{ESCALATE_S//60}m and still "
                          f"in-flight — likely wedged on an LLM call.", flush=True)
                continue
            st["retries"] += 1
            st["t0"] = now()
            cur = row(oid)
            st["base"] = str((cur or {}).get("updated_at") or "")[:19]
            print(f"[{ts()}] >>> RE-TRIGGERED {st['label']} "
                  f"(attempt {st['retries']+1}/{MAX_RETRIES+1}) HTTP {code} res={res}", flush=True)
            continue

        print(f"[{ts()}]  … {st['label']:15} {detail}", flush=True)

    if any(s["done"] is None for s in state.values()):
        time.sleep(POLL_S)

print(f"\n[{ts()}] all deals settled — running deep QA\n", flush=True)
out = []
for lbl, oid, _ in DEALS:
    st = state[oid]
    if not st["done"]:
        out.append({"label": lbl, "oid": oid, "status": "FAILED", "acc": "-"})
        continue
    r = st["done"]
    ds = r.get("scores") or {}
    hl = ds.get("headline") or {}
    acc = "?"
    try:
        p = subprocess.run([sys.executable, "qa_live.py", oid, lbl], capture_output=True,
                           text=True, timeout=300, env=QA_ENV)
        m = re.search(r"accuracy (\d+)%", (p.stdout or "") + (p.stderr or ""))
        acc = (m.group(1) + "%") if m else "?"
    except Exception as e:
        acc = f"qa-err:{type(e).__name__}"
    out.append({"label": lbl, "oid": oid, "status": "OK", "acc": acc,
                "win": hl.get("win_position"), "mom": hl.get("deal_momentum"),
                "commit": hl.get("customer_commitment"), "risk": hl.get("deal_risk"),
                "read": hl.get("read"), "src": ds.get("factor_source"),
                "reasons": ds.get("ai_reasons") or {},
                "ceo": r.get("ceo") or {}, "cov": r.get("cov") or {},
                "retries": st["retries"]})
json.dump(out, open("cc_work/_watchdog_results.json", "w"), indent=2, default=str)
print("WROTE cc_work/_watchdog_results.json", flush=True)
print("\n" + "=" * 92, flush=True)
for o in out:
    if o["status"] != "OK":
        print(f"{o['label']:16} FAILED"); continue
    print(f"{o['label']:16} WIN {o['win']:<5} MOM {o['mom']:<5} COMMIT {o['commit']:<5} "
          f"RISK {o['risk']:<5} {str(o['read']):<16} QA {o['acc']} retries={o['retries']}",
          flush=True)
print("ALL-DONE-MARKER", flush=True)
