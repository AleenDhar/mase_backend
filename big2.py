import warnings, time, datetime, threading
warnings.filterwarnings("ignore")
import requests, urllib3; urllib3.disable_warnings()
import sys; sys.stdout.reconfigure(encoding="utf-8", errors="replace")
cfg={}
for l in open(r'C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local',encoding='utf-8'):
    l=l.strip()
    if l and not l.startswith('#') and '=' in l:
        k,v=l.split('=',1); cfg[k.strip()]=v.strip().strip('"').strip("'")
API=cfg['DEAL_ENGINE_API_BASE'].rstrip('/'); AH={'Authorization':'Bearer '+cfg['DEAL_ENGINE_TOKEN'],'Content-Type':'application/json'}
SB=cfg['NEXT_PUBLIC_SUPABASE_URL'].rstrip('/'); K=cfg['SUPABASE_SERVICE_ROLE_KEY']; SH={'apikey':K,'Authorization':'Bearer '+K}
D=[('Robert Bosch','006P700000PlMpu'),('Austrian Post','006P700000J71MD')]
def ts(): return datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')
def rec(o):
    r=requests.get(SB+'/rest/v1/deal_records',params={'select':'updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio','opp_id':f'eq.{o}'},headers=SH,verify=False,timeout=60).json()[0]
    ds=r.get('scores') or {}; hl=ds.get('headline') or {}; sv=(r.get('studio') or {}).get('versions') or {}
    return r['updated_at'],bool(ds),hl.get('win_position'),hl.get('deal_momentum'),ds.get('factor_source'),sv.get('win')
def run(lbl,o):
    bu=rec(o)[0]
    threading.Thread(target=lambda: requests.post(f'{API}/api/deal-engine/sweep/{o}',headers=AH,json={},verify=False,timeout=(10,1500)),daemon=True).start()
    print(f'[{ts()}] -> {lbl}',flush=True)
    t0=time.time()
    while time.time()-t0<3600:
        time.sleep(45)
        up,pr,w,m,s,e=rec(o)
        if up!=bu and pr and w is not None:
            print(f"[{ts()}] OK {lbl:14} win={w} mom={m} v{e} src={s}",flush=True); return
        print(f'[{ts()}]  ... {lbl} ({int(time.time()-t0)//60}m)',flush=True)
    print(f'[{ts()}] TIMEOUT {lbl}',flush=True)
ts_a=[threading.Thread(target=run,args=d) for d in D]
for t in ts_a: t.start()
for t in ts_a: t.join()
print('BIG2-DONE',flush=True)
