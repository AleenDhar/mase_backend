import warnings, time, datetime, threading
warnings.filterwarnings("ignore")
import requests, urllib3; urllib3.disable_warnings()
cfg={}
for l in open(r'C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local',encoding='utf-8'):
    l=l.strip()
    if l and not l.startswith('#') and '=' in l:
        k,v=l.split('=',1); cfg[k.strip()]=v.strip().strip('"').strip("'")
API=cfg['DEAL_ENGINE_API_BASE'].rstrip('/'); AH={'Authorization':'Bearer '+cfg['DEAL_ENGINE_TOKEN'],'Content-Type':'application/json'}
SB=cfg['NEXT_PUBLIC_SUPABASE_URL'].rstrip('/'); K=cfg['SUPABASE_SERVICE_ROLE_KEY']; SH={'apikey':K,'Authorization':'Bearer '+K}
OID='006P700000H55TV'
def ts(): return datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')
def st():
    r=requests.get(f'{SB}/rest/v1/deal_records',params={'select':'updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio,cov:record->evidence_coverage','opp_id':f'eq.{OID}'},headers=SH,verify=False,timeout=60).json()
    if not r: return None
    r=r[0]; ds=r.get('scores') or {}; hl=ds.get('headline') or {}; sv=(r.get('studio') or {}).get('versions') or {}
    return {'upd':r.get('updated_at'),'win':hl.get('win_position'),'mom':hl.get('deal_momentum'),'read':hl.get('read'),'src':ds.get('factor_source'),'eng':sv.get('win'),'calls':(r.get('cov') or {}).get('calls_read')}
b=st(); bu=(b or {}).get('upd')
print(f"[{ts()}] Bandhan IN-PROCESS sweep on v10.8 (was win={(b or {}).get('win')} mom={(b or {}).get('mom')} v{(b or {}).get('eng')})",flush=True)
threading.Thread(target=lambda: requests.post(f'{API}/api/deal-engine/sweep/{OID}',headers=AH,json={},verify=False,timeout=(10,1500)),daemon=True).start()
t0=time.time()
while time.time()-t0<1600:
    time.sleep(40); a=st()
    if a and a['upd']!=bu and a['win'] is not None:
        ok=a['src']=='ai' and str(a['eng'])=='10.8'
        print(f"[{ts()}] BANDHAN DONE win={a['win']} mom={a['mom']} read={a['read']!r} v{a['eng']} src={a['src']} calls={a['calls']} {'GOVERNED' if ok else 'CHECK'}",flush=True); break
    print(f"[{ts()}]  … running ({int(time.time()-t0)//60}m)",flush=True)
print('BANDHAN-DONE',flush=True)
