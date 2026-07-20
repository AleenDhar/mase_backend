"""PRACTICE-SET live cloud sweeps + deep QA (user-gated step before the forecasted fleet).

For each practice deal (already in the book): trigger a LIVE manual sweep on the deployed
pipeline (writes deal_records), wait for swept_at to advance, then run qa_live.py on the row.
Runs BATCH=2 concurrent to keep the 2-task API comfortable. Prints a final accuracy table.
"""
import subprocess, sys, time
import dryrun_fleet as D
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PRACTICE = [
    ("Allstate",          "006P7000006uKrq"),
    ("Cebu Pacific Air",  "0066700000wdNe1"),
    ("Consumer Cellular", "006P700000OcxpH"),
    ("Publicis Groupe",   "006P700000Xl06R"),
    ("John Deere",        "006P700000KHd9V"),
]
BATCH = 2
TIMEOUT_S = 1800


def swept_at(oid):
    r = D.requests.get(f"{D.SB}/rest/v1/deal_records", params={"select": "swept_at", "opp_id": f"eq.{oid}"},
                       headers=D.SH, verify=D.VERIFY, timeout=60).json()
    return (r[0].get("swept_at") if isinstance(r, list) and r else None)


def trigger(oid):
    r = D.requests.post(f"{D.API}/api/deal-engine/sweep/trigger",
                        headers={**D.AH, "Content-Type": "application/json"},
                        json={"opp_id": oid, "source": "manual"}, verify=False, timeout=60)
    return r.status_code


results = []
queue = list(PRACTICE)
inflight = {}
while queue or inflight:
    while queue and len(inflight) < BATCH:
        label, oid = queue.pop(0)
        base = swept_at(oid)
        code = trigger(oid)
        print(f"[trigger] {label:18} {oid} -> HTTP {code} (base swept_at={base})", flush=True)
        inflight[oid] = {"label": label, "base": base, "t0": time.time()}
        time.sleep(5)
    time.sleep(45)
    for oid in list(inflight):
        st = inflight[oid]
        cur = swept_at(oid)
        age = int(time.time() - st["t0"])
        if cur and cur != st["base"]:
            print(f"\n[done] {st['label']} updated (swept_at={cur}, {age}s) — running deep QA", flush=True)
            p = subprocess.run([sys.executable, "qa_live.py", oid, st["label"]],
                               capture_output=True, text=True, timeout=180)
            out = (p.stdout or "") + (p.stderr or "")
            print(out, flush=True)
            import re as _re
            m = _re.search(r"PASS (\d+) / FAIL (\d+) / WARN (\d+)\s+->\s+accuracy (\d+)%", out)
            results.append({"label": st["label"], "opp_id": oid,
                            "pass": m and m.group(1), "fail": m and m.group(2),
                            "warn": m and m.group(3), "accuracy": (m.group(4) + "%") if m else "?"})
            del inflight[oid]
        elif age > TIMEOUT_S:
            print(f"[TIMEOUT] {st['label']} after {age}s", flush=True)
            results.append({"label": st["label"], "opp_id": oid, "accuracy": "TIMEOUT"})
            del inflight[oid]
        else:
            print(f"  … {st['label']} running ({age // 60}m)", flush=True)

print("\n========== PRACTICE-SET SCORECARD ==========", flush=True)
for r in results:
    print(f"  {r['label']:20} {r['opp_id']}  accuracy={r['accuracy']} "
          f"(pass={r.get('pass')} fail={r.get('fail')} warn={r.get('warn')})", flush=True)
