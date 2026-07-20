"""Definitive worker-image check on a LOW-STAKES deal (MTR, currently a stale score).
Wait for the queue to drain (old worker scales to 0) -> enqueue -> read the run's model.
sonnet-5 + non-null = worker healed. sonnet-4-5/null = still stale -> restore MTR in-process."""
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
OID='006P700000KTTO5'  # MTR
def ts(): return datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')
def utcnow(): return datetime.datetime.now(datetime.timezone.utc)
# 1. wait for queue drain (<=3 min)
t0=time.time()
while time.time()-t0<180:
    q=requests.get(SB+'/rest/v1/sweep_queue',params={'select':'status','status':'in.(waiting,working)'},headers=SH,verify=False,timeout=60).json()
    n=len(q) if isinstance(q,list) else 0
    print(f'[{ts()}] queue active rows={n}',flush=True)
    if n==0: break
    time.sleep(30)
print(f'[{ts()}] queue drained; waiting 75s for the old worker to scale to 0…',flush=True); time.sleep(75)
# 2. enqueue MTR via worker
start=utcnow()
r=requests.post(f'{API}/api/deal-engine/sweep/trigger',headers=AH,json={'opp_id':OID,'source':'manual'},verify=False,timeout=60)
res=((r.json() or {}).get('results') or {}).get(OID)
print(f'[{ts()}] enqueue MTR via worker: {res}',flush=True)
# 3. watch the run
while time.time()-t0<1600:
    time.sleep(40)
    runs=requests.get(SB+'/rest/v1/deal_trigger_runs',params={'select':'status,model,error,created_at','opp_id':f'eq.{OID}','created_at':f'gte.{start.isoformat()}','order':'created_at.desc','limit':'2'},headers=SH,verify=False,timeout=60).json()
    fin=[x for x in (runs or []) if (x.get('status') or '').lower() in ('completed','failed')]
    if fin:
        run=fin[0]; model=run.get('model') or ''
        rec=requests.get(SB+'/rest/v1/deal_records',params={'select':'scores:record->ai->deal_scores,studio:record->ai->scoring_studio','opp_id':f'eq.{OID}'},headers=SH,verify=False,timeout=60).json()
        ds=(rec[0].get('scores') or {}) if rec else {}; hl=ds.get('headline') or {}; sv=(rec[0].get('studio') or {}).get('versions') or {} if rec else {}
        ok = 'claude-sonnet-5' in model and hl.get('win_position') is not None and ds.get('factor_source')=='ai'
        print(f"[{ts()}] MTR run: status={run.get('status')} model={model} win={hl.get('win_position')} src={ds.get('factor_source')} engine=v{sv.get('win')}",flush=True)
        if ok:
            print(f'[{ts()}] WORKER-OK — worker healed to the current image; the 20-wide fleet path is safe.',flush=True)
        else:
            print(f'[{ts()}] WORKER-STALE — still writing bad scores. Restoring MTR in-process; do NOT use the worker path.',flush=True)
            def fix():
                try: requests.post(f'{API}/api/deal-engine/sweep/{OID}',headers=AH,json={},verify=False,timeout=(10,1500))
                except Exception: pass
            threading.Thread(target=fix,daemon=True).start()
            time.sleep(600)
        break
    print(f'[{ts()}]  … MTR running ({int(time.time()-t0)//60}m)',flush=True)
print('WORKER-RECHECK-DONE',flush=True)
