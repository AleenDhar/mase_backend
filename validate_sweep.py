"""Fire 5 REAL single-opp sweeps (deployed backend, Opus) in parallel to validate
the native CEO discriminator + title/name guardrails end-to-end. Prints status per
deal. ~11 min each; run in background."""
import sys, time, requests, urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from daily_summary.common import load_secret
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ALB = "http://mase-alb-1262623499.ap-south-1.elb.amazonaws.com"
sec = load_secret()
TOK = sec.get("DISPATCH_SECRET")

DEALS = [
    ("006P700000J71MD", "Austrian Post (title gate + CEO-eligible, stalling)"),
    ("006P700000Xl06R", "Publicis (CEO-eligible, CPO target)"),
    ("006P700000PlMpu", "Robert Bosch (marquee $1.2M)"),
    ("006P700000PtQGP", "Mair (closed-won / Contract In Progress)"),
    ("006P700000X6hvK", "Domino's (NON-forecasted Pipeline win 62 -> now eligible, tests gate)"),
]


def sweep(opp, label):
    t0 = time.time()
    try:
        r = requests.post(f"{ALB}/api/deal-engine/sweep/{opp}",
                          headers={"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"},
                          json={}, verify=False, timeout=1600)
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text[:200]}
        return opp, label, r.status_code, j.get("status"), round(time.time() - t0), j.get("error")
    except Exception as e:
        return opp, label, "ERR", str(e)[:120], round(time.time() - t0), None


def main():
    print(f"firing {len(DEALS)} real sweeps in parallel (deployed backend, Opus)…", flush=True)
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = [ex.submit(sweep, o, l) for o, l in DEALS]
        for f in as_completed(futs):
            opp, label, code, status, dur, err = f.result()
            print(f"[done] {opp} | {label[:40]:40} | HTTP {code} | status={status} | {dur}s"
                  + (f" | err={err}" if err else ""), flush=True)
    print("ALL SWEEPS RETURNED", flush=True)


if __name__ == "__main__":
    main()
