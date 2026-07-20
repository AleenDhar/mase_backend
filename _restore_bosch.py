import warnings, time, datetime, threading
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
cfg={}
for l in open(r'C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local',encoding='utf-8'):
    l=l.strip()
    if l and not l.startswith('#') and '=' in l:
        k,v=l.split('=',1); cfg[k.strip()]=v.strip().strip('"').strip("'")
API=cfg['DEAL_ENGINE_API_BASE'].rstrip('/'); AH={'Authorization':'Bearer '+cfg['DEAL_ENGINE_TOKEN'],'Content-Type':'application/json'}
SB=cfg['NEXT_PUBLIC_SUPABASE_URL'].rstrip('/'); K=cfg['SUPABASE_SERVICE_ROLE_KEY']; SH={'apikey':K,'Authorization':'Bearer '+K}
OID='006P700000PlMpu'
def ts(): return datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')
print(f'[{ts()}] IN-PROCESS restore Bosch (bypasses the stale worker)',flush=True)
def fire():
    try: requests.post(f'{API}/api/deal-engine/sweep/{OID}',headers=AH,json={},verify=False,timeout=(10,1500))
    except Exception: pass
threading.Thread(target=fire,daemon=True).start()
t0=time.time()
while time.time()-t0<1500:
    time.sleep=__import__('time').sleep; time.sleep(40)
    r=requests.get(f'{SB}/rest/v1/deal_records',params={'select':'updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio','opp_id':f'eq.{OID}'},headers=SH,verify=False,timeout=60).json()
    ds=(r[0].get('scores') or {}) if r else {}; hl=ds.get('headline') or {}; sv=(r[0].get('studio') or {}).get('versions') or {} if r else {}
    if ds and hl.get('win_position') is not None and ds.get('factor_source')=='ai':
        print(f"[{ts()}] RESTORED Bosch win={hl.get('win_position')} mom={hl.get('deal_momentum')} v{sv.get('win')} src={ds.get('factor_source')}",flush=True); break
    print(f'[{ts()}]  … restoring ({int(time.time()-t0)//60}m)',flush=True)
print('BOSCH-RESTORE-DONE',flush=True)
