import sys, time, warnings, datetime
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
SB=cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/"); K=cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH={"apikey":K,"Authorization":f"Bearer {K}"}
OID="006P700000X0TlP"  # E&Y_UK
SEL="updated_at,eng:record->ai->scoring_studio->versions->win,w:record->ai->deal_scores->headline->win_position"
def ts(): return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
def rec():
    return (requests.get(f"{SB}/rest/v1/deal_records",params={"select":SEL,"opp_id":f"eq.{OID}"},headers=SH,verify=False,timeout=(10,60)).json() or [{}])[0]
base=rec(); bu=base.get("updated_at")
print(f"[{ts()}] E&Y_UK retry (was v{base.get('eng')} win={base.get('w')})",flush=True)
try: requests.post(f"{API}/api/deal-engine/sweep/{OID}",headers=AH,json={},verify=False,timeout=(10,2000))
except Exception as e: print(f"post err {e}",flush=True)
t0=time.time()
while time.time()-t0<3000:
    time.sleep(30)
    try: a=rec()
    except Exception: continue
    if a.get("updated_at")!=bu and a.get("w") is not None:
        print(f"[{ts()}] E&Y_UK DONE win={a.get('w')} v{a.get('eng')}",flush=True)
        print("RETRY-EY-DONE-v10.10" if str(a.get('eng'))=="10.10" else f"RETRY-EY-CHECK-eng={a.get('eng')}",flush=True)
        break
else:
    print(f"[{ts()}] E&Y_UK RETRY-TIMEOUT again (pathologically heavy deal — needs manual look)",flush=True)
