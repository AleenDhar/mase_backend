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
OID='006P700000H55TV'
def ts(): return datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')
def rec():
    r=requests.get(f'{SB}/rest/v1/deal_records',params={'select':'updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio,daysum:record->ai->day_summary','opp_id':f'eq.{OID}'},headers=SH,verify=False,timeout=90).json()
    return r[0] if r else None
b=rec(); bu=(b or {}).get('updated_at')
print(f"[{ts()}] Bandhan IN-PROCESS restore (API tier, correct image)",flush=True)
threading.Thread(target=lambda: requests.post(f'{API}/api/deal-engine/sweep/{OID}',headers=AH,json={},verify=False,timeout=(10,1500)),daemon=True).start()
t0=time.time()
while time.time()-t0<1500:
    time.sleep=__import__('time').sleep; time.sleep(40); r=rec()
    if r and r.get('updated_at')!=bu:
        ds=r.get('scores') or {}; hl=ds.get('headline') or {}; sv=(r.get('studio') or {}).get('versions') or {}; dsum=r.get('daysum') or {}
        print(f"[{ts()}] RESTORED win={hl.get('win_position')} mom={hl.get('deal_momentum')} v{sv.get('win')} src={ds.get('factor_source')} summary_source={dsum.get('source')}",flush=True)
        print(f"[{ts()}] summary: {(dsum.get('overall') or '')[:200]}",flush=True)
        break
    print(f"[{ts()}]  ... restoring ({int(time.time()-t0)//60}m)",flush=True)
print('BANDHAN2-DONE',flush=True)
