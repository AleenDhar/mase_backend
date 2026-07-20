import sys, time, json, warnings, datetime
warnings.filterwarnings("ignore")
import requests, urllib3; urllib3.disable_warnings()
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
cfg={}
for l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local",encoding="utf-8"):
    l=l.strip()
    if l and not l.startswith("#") and "=" in l:
        k,v=l.split("=",1); cfg[k.strip()]=v.strip().strip('"').strip("'")
API=cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
AH={"Authorization":f"Bearer {cfg['DEAL_ENGINE_TOKEN']}","Content-Type":"application/json"}
def ts(): return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
print(f"[{ts()}] POST /api/deal-engine/hard-refresh (delete_initial_interest=false)…", flush=True)
try:
    r=requests.post(f"{API}/api/deal-engine/hard-refresh",headers=AH,
                    json={"delete_initial_interest": False}, verify=False, timeout=(10,1500))
    print(f"[{ts()}] HTTP {r.status_code}")
    try: print(json.dumps(r.json(), indent=2)[:1200])
    except Exception: print(r.text[:800])
except Exception as e:
    print(f"[{ts()}] request err {type(e).__name__}: {e}")
print("HARDREFRESH-DONE", flush=True)
