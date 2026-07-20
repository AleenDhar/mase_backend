"""LIVE cloud sweep of the 6 forecasted deals, 6 in parallel, then deep QA on each row.

Fires POST /api/deal-engine/sweep/trigger {opp_id, source:"manual"} for all six at once
(the deployed API runs manual triggers on the web tier; blue is scaled out so each lands
on its own task). Waits on deal_records.updated_at (timestamptz -> same-day safe), then
runs qa_live.py per deal and prints a scorecard + before/after score delta vs the local CSV.

Usage:
  python six_live.py            # trigger + watch + QA
  python six_live.py baseline   # read-only: print current state, no writes
"""
import csv, subprocess, sys, time, re, datetime, json
import boot_env  # noqa: F401  — MUST precede dryrun_fleet (kills the per-process AWS stall)
import dryrun_fleet as D

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SIX = [
    ("SAMI",          "006P700000RD9Ir"),
    ("Allstate",      "006P7000006uKrq"),
    ("Robert Bosch",  "006P700000PlMpu"),
    ("NORTHPORT",     "006P700000QFJwD"),
    ("Domino's Pizza", "006P700000X6hvK"),
    ("Greencore",     "006P700000WeRX8"),
]
# Live green = 2 tasks @ 1 vCPU/2 GB on the pre-fix image (fire-and-forget in-process
# sweeps). The 2026-07-09 incident OOM'd at 3 concurrent sweeps on one such task, so we
# hard-cap IN-FLIGHT triggers at 2 (the pattern practice_live.py proved safe this session)
# and fully drain each pair before firing the next. Override with SIX_BATCH if green is scaled.
import os as _os
BATCH = int(_os.environ.get("SIX_BATCH", "2"))
TIMEOUT_S = 2400
POLL_S = 40


# NOTE: never `select=record` — the blob is multi-MB per deal and the read stalls behind
# Zscaler. Project only the nested JSON paths we actually need (PostgREST `alias:col->a->b`).
SELECT = "updated_at,swept_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio"


def row(oid, select=SELECT):
    r = D.requests.get(f"{D.SB}/rest/v1/deal_records",
                       params={"select": select, "opp_id": f"eq.{oid}"},
                       headers=D.SH, verify=D.VERIFY, timeout=(10, 45)).json()
    return r[0] if isinstance(r, list) and r else None


def state(oid):
    r = row(oid)
    if not r:
        return None
    ds = r.get("scores") or {}
    hl = ds.get("headline") or {}
    sv = (r.get("studio") or {}).get("versions") or {}
    return {
        "updated_at": r.get("updated_at"), "swept_at": r.get("swept_at"),
        "win": hl.get("win_position"), "mom": hl.get("deal_momentum"),
        "commit": hl.get("customer_commitment"), "risk": hl.get("deal_risk"),
        "src": ds.get("factor_source"), "win_engine": sv.get("win"), "mom_engine": sv.get("mom"),
    }


def trigger(oid):
    r = D.requests.post(f"{D.API}/api/deal-engine/sweep/trigger",
                        headers={**D.AH, "Content-Type": "application/json"},
                        json={"opp_id": oid, "source": "manual"}, verify=False, timeout=60)
    return r.status_code, r.text[:120]


def local_scores():
    out = {}
    try:
        for r in csv.DictReader(open("cc_fleet_results.csv", encoding="utf-8-sig")):
            out[r["opp_id"]] = (r["win"], r["momentum"], r["commitment"], r["risk"])
    except Exception:
        pass
    return out


if __name__ == "__main__":
    LOCAL = local_scores()
    if len(sys.argv) > 1 and sys.argv[1] == "baseline":
        print("BASELINE (read-only) — current deal_records state\n")
        for label, oid in SIX:
            s = state(oid)
            if not s:
                print(f"  {label:15} NO ROW"); continue
            lw = LOCAL.get(oid, ("-",) * 4)
            print(f"  {label:15} upd={str(s['updated_at'])[:19]} win={s['win']} mom={s['mom']} "
                  f"src={s['src']} winEng=v{s['win_engine']}   | local-csv win={lw[0]} mom={lw[1]}")
        sys.exit(0)

    print(f"=== LIVE SWEEP: {len(SIX)} deals, hard-cap {BATCH} in-flight "
          f"(drain each wave before firing next) ===\n", flush=True)
    # NORTHPORT first — its live deal_scores is NULL (stale-worker wipe), so repairing the
    # broken front-end row is the highest-value single trigger.
    queue = sorted(SIX, key=lambda x: 0 if x[0].startswith("NORTHPORT") else 1)
    inflight = {}
    results = []

    def fire(label, oid):
        base = state(oid)
        code, body = trigger(oid)
        print(f"[trigger] {label:15} {oid} -> HTTP {code} {body if code >= 300 else ''}", flush=True)
        inflight[oid] = {"label": label, "base": (base or {}).get("updated_at"), "t0": time.time()}

    while queue and len(inflight) < BATCH:
        fire(*queue.pop(0)); time.sleep(3)
    print(f"\n[watch] polling every {POLL_S}s (per-deal timeout {TIMEOUT_S // 60}m); "
          f"{len(queue)} deal(s) queued behind the cap\n", flush=True)

    while inflight or queue:
        time.sleep(POLL_S)
        for oid in list(inflight):
            st = inflight[oid]
            cur = state(oid)
            age = int(time.time() - st["t0"])
            if cur and cur.get("updated_at") != st["base"]:
                print(f"\n[done {age // 60}m{age % 60:02d}s] {st['label']} — win={cur['win']} mom={cur['mom']} "
                      f"commit={cur['commit']} risk={cur['risk']} src={cur['src']} winEng=v{cur['win_engine']}",
                      flush=True)
                p = subprocess.run([sys.executable, "qa_live.py", oid, st["label"]],
                                   capture_output=True, text=True, timeout=240)
                out = (p.stdout or "") + (p.stderr or "")
                print(out, flush=True)
                m = re.search(r"PASS (\d+) / FAIL (\d+) / WARN (\d+)\s+->\s+accuracy (\d+)%", out)
                results.append({"label": st["label"], "oid": oid, "sec": age, "cur": cur,
                                "pass": m.group(1) if m else "?", "fail": m.group(2) if m else "?",
                                "warn": m.group(3) if m else "?",
                                "acc": (m.group(4) + "%") if m else "?"})
                del inflight[oid]
            elif age > TIMEOUT_S:
                print(f"[TIMEOUT] {st['label']} after {age // 60}m", flush=True)
                results.append({"label": st["label"], "oid": oid, "sec": age, "cur": cur or {},
                                "acc": "TIMEOUT", "pass": "-", "fail": "-", "warn": "-"})
                del inflight[oid]
            else:
                print(f"  … {st['label']:15} running {age // 60}m{age % 60:02d}s", flush=True)
        # refill the pipe up to the cap as slots free (keeps exactly BATCH in flight)
        while queue and len(inflight) < BATCH:
            label, oid = queue.pop(0)
            fire(label, oid)
            print(f"       (wave refill: {len(queue)} still queued)", flush=True)
            time.sleep(3)

    print("\n" + "=" * 100, flush=True)
    print("LIVE 6-DEAL SCORECARD  (cloud run vs local CSV)", flush=True)
    print("=" * 100, flush=True)
    print(f"{'deal':16} {'cloud win/mom':>14} {'local win/mom':>14} {'src':>7} {'winEng':>7} "
          f"{'mins':>5} {'accuracy':>9}  P/F/W", flush=True)
    for r in sorted(results, key=lambda x: x["label"]):
        c = r["cur"]
        lw = LOCAL.get(r["oid"], ("-", "-", "-", "-"))
        print(f"{r['label']:16} {str(c.get('win')) + '/' + str(c.get('mom')):>14} "
              f"{lw[0] + '/' + lw[1]:>14} {str(c.get('src')):>7} {'v' + str(c.get('win_engine')):>7} "
              f"{r['sec'] // 60:>5} {r['acc']:>9}  {r['pass']}/{r['fail']}/{r['warn']}", flush=True)
    json.dump(results, open("cc_work/_six_live.json", "w"), indent=2, default=str)
    print("\nwrote cc_work/_six_live.json", flush=True)
