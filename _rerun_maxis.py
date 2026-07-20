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
OID="006P700000W5OP3"
SEL=("updated_at,eng:record->ai->scoring_studio->versions->win,"
     "w:record->ai->deal_scores->headline->win_position,m:record->ai->deal_scores->headline->deal_momentum,"
     "rd:record->ai->deal_scores->headline->read,"
     "champ:record->ai->meddpicc->champion->status,eb:record->ai->meddpicc->economic_buyer->status")
def ts(): return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
def rec(): return (requests.get(f"{SB}/rest/v1/deal_records",params={"select":SEL,"opp_id":f"eq.{OID}"},headers=SH,verify=False,timeout=(10,60)).json() or [{}])[0]
b=rec(); bu=b.get("updated_at")
print(f"[{ts()}] Maxis BEFORE: win={b.get('w')} mom={b.get('m')} read={b.get('rd')} v{b.get('eng')} | champ={b.get('champ')} eb={b.get('eb')}",flush=True)
try: requests.post(f"{API}/api/deal-engine/sweep/{OID}",headers=AH,json={},verify=False,timeout=(10,2000))
except Exception as e: print("post err",e,flush=True)
t0=time.time()
while time.time()-t0<2600:
    time.sleep(30)
    try: a=rec()
    except Exception: continue
    if a.get("updated_at")!=bu and a.get("w") is not None:
        print(f"[{ts()}] Maxis AFTER : win={a.get('w')} mom={a.get('m')} read={a.get('rd')} v{a.get('eng')} | champ={a.get('champ')} eb={a.get('eb')}",flush=True)
        print("RERUN-MAXIS-DONE",flush=True); break
else:
    print(f"[{ts()}] RERUN-MAXIS-TIMEOUT",flush=True)
