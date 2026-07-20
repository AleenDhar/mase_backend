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
OID='006P700000UGPE5'
def ts(): return datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')
def rec():
    r=requests.get(f'{SB}/rest/v1/deal_records',params={'select':'updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio,cov:record->evidence_coverage','opp_id':f'eq.{OID}'},headers=SH,verify=False,timeout=60).json()[0]
    ds=r.get('scores') or {}; hl=ds.get('headline') or {}; sv=(r.get('studio') or {}).get('versions') or {}
    return r['updated_at'], bool(ds), hl.get('win_position'), hl.get('deal_momentum'), hl.get('read'), ds.get('factor_source'), sv.get('win'), (r.get('cov') or {}).get('calls_read')
bu,_,w0,m0,_,_,e0,_=rec()
print(f'[{ts()}] Etex BEFORE win={w0} mom={m0} v{e0}',flush=True)
threading.Thread(target=lambda: requests.post(f'{API}/api/deal-engine/sweep/{OID}',headers=AH,json={},verify=False,timeout=(10,1400)),daemon=True).start()
t0=time.time()
while time.time()-t0<1600:
    time.sleep=__import__('time').sleep; time.sleep(35)
    up,present,w,m,rd,src,eng,calls=rec()
    if up!=bu and present and w is not None:
        print(f'[{ts()}] Etex AFTER  win={w} mom={m} read={rd!r} v{eng} src={src} calls={calls}',flush=True)
        print(f'[{ts()}] delta: win {w0}->{w}  mom {m0}->{m}',flush=True)
        break
    print(f'[{ts()}]  ... sweeping ({int(time.time()-t0)//60}m)',flush=True)
print('ETEX-DONE',flush=True)
