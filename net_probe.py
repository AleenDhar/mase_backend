import sys, time, socket, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

print("py", sys.version.split()[0], flush=True)
t = time.time()
from daily_summary.common import load_secret, VERIFY
print(f"import common {time.time()-t:.1f}s  VERIFY={VERIFY}", flush=True)

ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"
cfg = {}
for line in open(ENV, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
API = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
sec = load_secret()
SB = sec["SUPABASE_URL"].rstrip("/")
SKEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
SH = {"apikey": SKEY, "Authorization": f"Bearer {SKEY}"}
print("API =", API, flush=True)
print("SB  =", SB, flush=True)

host = SB.split("//", 1)[1].split("/")[0]
for label, h, p in [("supabase", host, 443),
                    ("alb", API.split("//", 1)[1].split("/")[0].split(":")[0], 80)]:
    t = time.time()
    try:
        s = socket.create_connection((h, p), timeout=8); s.close()
        print(f"[tcp ok ] {label:9} {h}:{p}  {time.time()-t:.1f}s", flush=True)
    except Exception as e:
        print(f"[tcp ERR] {label:9} {h}:{p}  {e}", flush=True)

t = time.time()
try:
    r = requests.get(f"{SB}/rest/v1/deal_records",
                     params={"select": "opp_id", "limit": "1"},
                     headers=SH, verify=VERIFY, timeout=(10, 25))
    print(f"[sb VERIFY ] {r.status_code} {time.time()-t:.1f}s {r.text[:80]}", flush=True)
except Exception as e:
    print(f"[sb VERIFY ] ERR {time.time()-t:.1f}s {type(e).__name__}: {e}", flush=True)

t = time.time()
try:
    r = requests.get(f"{SB}/rest/v1/deal_records",
                     params={"select": "opp_id", "limit": "1"},
                     headers=SH, verify=False, timeout=(10, 25))
    print(f"[sb noverify] {r.status_code} {time.time()-t:.1f}s {r.text[:80]}", flush=True)
except Exception as e:
    print(f"[sb noverify] ERR {time.time()-t:.1f}s {type(e).__name__}: {e}", flush=True)

t = time.time()
try:
    r = requests.get(f"{API}/api/health", verify=False, timeout=(10, 25))
    print(f"[api health ] {r.status_code} {time.time()-t:.1f}s {r.text[:120]}", flush=True)
except Exception as e:
    print(f"[api health ] ERR {time.time()-t:.1f}s {type(e).__name__}: {e}", flush=True)
