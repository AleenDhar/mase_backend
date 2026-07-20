"""Read-only: how many distinct API processes sit behind the ALB?

Each ECS task is its own Python process with its own module-level state. GET
/api/deal-engine/sweep/status returns `started_at` (that process's boot time), so hammering
the ALB and collecting distinct started_at values counts the live tasks — without the AWS CLI
(which hangs behind Zscaler). This matters because the trigger semaphore
(DEAL_TRIGGER_CONCURRENCY, default 3) is PER PROCESS: real parallelism = 3 x tasks-hit.
"""
import sys, collections, warnings
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
AH = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}", "Connection": "close"}

seen = collections.Counter()
N = 24
for i in range(N):
    try:
        r = requests.get(f"{API}/api/deal-engine/sweep/status", headers=AH,
                         verify=False, timeout=(6, 20))
        st = r.json()
        seen[str(st.get("started_at"))] += 1
    except Exception as e:
        seen[f"ERR {type(e).__name__}"] += 1
print(f"{N} probes -> {len(seen)} distinct API process(es) behind the ALB:\n")
for k, v in seen.most_common():
    print(f"  hits={v:>3}  started_at={k}")
print(f"\n  effective trigger parallelism = 3 (per-process sem) x {len(seen)} task(s) "
      f"= up to {3*len(seen)} concurrent sweeps")
print("  (7 POSTs were round-robined across these tasks; each task runs at most 3 at once)")
