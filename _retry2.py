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
SEL="updated_at,eng:record->ai->scoring_studio->versions->win,w:record->ai->deal_scores->headline->win_position,m:record->ai->deal_scores->headline->deal_momentum"
DEALS=[("KWSP","006P700000Wkphs"),("KPJ","006P700000URsB7")]
def ts(): return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
def rec(o): return (requests.get(f"{SB}/rest/v1/deal_records",params={"select":SEL,"opp_id":f"eq.{o}"},headers=SH,verify=False,timeout=(10,60)).json() or [{}])[0]
from concurrent.futures import ThreadPoolExecutor
def go(name,oid):
    b=rec(oid); bu=b.get("updated_at")
    try: requests.post(f"{API}/api/deal-engine/sweep/{oid}",headers=AH,json={},verify=False,timeout=(10,2000))
    except Exception: pass
    t0=time.time()
    while time.time()-t0<2600:
        time.sleep(30)
        try: a=rec(oid)
        except Exception: continue
        if a.get("updated_at")!=bu and a.get("w") is not None:
            print(f"[{ts()}] {name} retry -> win={a.get('w')} mom={a.get('m')} v{a.get('eng')} {'OK' if str(a.get('eng'))=='10.10' else 'STILL-OFF'}",flush=True)
            return
    print(f"[{ts()}] {name} retry TIMEOUT",flush=True)
with ThreadPoolExecutor(max_workers=2) as ex: list(ex.map(lambda d: go(*d), DEALS))
print("RETRY2-DONE",flush=True)
