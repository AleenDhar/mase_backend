"""FORECASTED-FLEET live run (GATED — run only after the practice set passes and the user says go,
AND the manual-only-lift deploy has landed so the queue+worker fleet is active).

  python fleet_run.py            # dry list: how many deals would run
  python fleet_run.py --go       # enqueue ALL active forecast_critical deals + watch the drain
  python fleet_run.py --go --qa  # after the drain, deep-QA a 6-deal sample via qa_live.py

Mechanism: POST /api/deal-engine/sweep/trigger in chunks of 25 (source=manual). With
DEAL_SWEEP_MANUAL_ONLY=false the server ENQUEUES to sweep_queue; the autoscaler sizes
mase-worker to the backlog (8 concurrent per worker, max 6 workers).
"""
import sys, time, random
from collections import Counter
import dryrun_fleet as D
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

GO = "--go" in sys.argv
QA = "--qa" in sys.argv

rows = D.forecasted()
ids = [r["opp_id"] for r in rows]
print(f"forecasted fleet: {len(ids)} active forecast_critical deals", flush=True)
if not GO:
    for r in rows[:10]:
        print(f"  {r.get('account_name','')[:30]:30} {r['opp_id']} {r.get('stage')}")
    print(f"  … (+{max(0, len(rows) - 10)} more)  — re-run with --go to start")
    sys.exit(0)

# sanity: refuse if manual-only is still active (the trigger would run these on the web tier)
probe = D.requests.post(f"{D.API}/api/deal-engine/sweep/trigger",
                        headers={**D.AH, "Content-Type": "application/json"},
                        json={"opp_ids": [], "source": "manual"}, verify=False, timeout=30)
# (empty list -> 400; we only care that the API is up)
print(f"API probe {probe.status_code}", flush=True)

CHUNK = 25
for i in range(0, len(ids), CHUNK):
    chunk = ids[i:i + CHUNK]
    r = D.requests.post(f"{D.API}/api/deal-engine/sweep/trigger",
                        headers={**D.AH, "Content-Type": "application/json"},
                        json={"opp_ids": chunk, "source": "manual"}, verify=False, timeout=120)
    body = r.json() if r.text else {}
    vals = list((body.get("results") or {}).values())
    print(f"[enqueue {i//CHUNK + 1}] {len(chunk)} deals -> HTTP {r.status_code} "
          f"{Counter(str(v) for v in vals)}", flush=True)
    time.sleep(3)

print("\nwatching sweep_queue drain (autoscaled workers)…", flush=True)
t0 = time.time()
while time.time() - t0 < 4 * 3600:
    time.sleep(120)
    q = D.requests.get(f"{D.SB}/rest/v1/sweep_queue",
                       params={"select": "status", "opp_id": f"in.({','.join(ids)})"},
                       headers=D.SH, verify=D.VERIFY, timeout=120).json()
    by = Counter(x.get("status") for x in q)
    rem = by.get("waiting", 0) + by.get("working", 0)
    print(f"  [{int((time.time()-t0)//60):3d}m] {dict(by)}  remaining={rem}", flush=True)
    if rem == 0:
        print("QUEUE DRAINED.", flush=True)
        break

if QA:
    import subprocess
    sample = random.sample(rows, min(6, len(rows)))
    print("\n=== post-fleet QA sample ===", flush=True)
    for r in sample:
        p = subprocess.run([sys.executable, "qa_live.py", r["opp_id"], r.get("account_name") or r["opp_id"]],
                           capture_output=True, text=True, timeout=180)
        for ln in (p.stdout or "").splitlines():
            if "accuracy" in ln or "LIVE QA" in ln:
                print("  " + ln.strip(), flush=True)
print("\nFLEET RUN COMPLETE", flush=True)
