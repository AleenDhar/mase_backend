import warnings,time,datetime,threading
warnings.filterwarnings("ignore")
import requests,urllib3; urllib3.disable_warnings()
cfg={}
for l in open(r'C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local',encoding='utf-8'):
    l=l.strip()
    if l and not l.startswith('#') and '=' in l:
        k,v=l.split('=',1); cfg[k.strip()]=v.strip().strip('"').strip("'")
API=cfg['DEAL_ENGINE_API_BASE'].rstrip('/'); AH={'Authorization':'Bearer '+cfg['DEAL_ENGINE_TOKEN'],'Content-Type':'application/json'}
SB=cfg['NEXT_PUBLIC_SUPABASE_URL'].rstrip('/'); K=cfg['SUPABASE_SERVICE_ROLE_KEY']; SH={'apikey':K,'Authorization':'Bearer '+K}
OID='006P700000QKfzN'
def ts(): return datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')
def rec():
    for _ in range(4):
        try:
            r=requests.get(SB+'/rest/v1/deal_records',params={'select':'updated_at,scores:record->ai->deal_scores','opp_id':f'eq.{OID}'},headers=SH,verify=False,timeout=30).json()[0]
            ds=r.get('scores') or {}; hl=ds.get('headline') or {}
            return r['updated_at'],bool(ds),hl.get('win_position'),hl.get('deal_momentum'),ds.get('factor_source')
        except Exception: time.sleep(6)
    return None,False,None,None,None
bu=rec()[0]
print(f"[{ts()}] Scheme SOLO in-process sweep (no contention)",flush=True)
threading.Thread(target=lambda: requests.post(f'{API}/api/deal-engine/sweep/{OID}',headers=AH,json={},verify=False,timeout=(10,1500)),daemon=True).start()
t0=time.time()
while time.time()-t0<2400:
    time.sleep(40); up,pr,win,mom,src=rec()
    if up and up!=bu and pr and win is not None:
        print(f"[{ts()}] SCHEME DONE win={win} mom={mom} src={src}",flush=True); break
    print(f"[{ts()}]  ... ({int(time.time()-t0)//60}m)",flush=True)
else:
    print(f"[{ts()}] SCHEME STILL TIMED OUT — needs log investigation",flush=True)
print('SCHEME-DONE',flush=True)
