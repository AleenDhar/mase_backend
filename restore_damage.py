"""Undo damage: restore the 2 nulled deals via the HEALED worker (enqueue -> worker fleet).
Verifies model=sonnet-5, non-null v10.8 scores, human summary (source=ai)."""
import warnings, time, datetime
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
DEALS=[("Bandhan Bank","006P700000H55TV"),("NORTHPORT","006P700000QFJwD")]
def ts(): return datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')
def now(): return datetime.datetime.now(datetime.timezone.utc)
def rec(oid):
    r=requests.get(f'{SB}/rest/v1/deal_records',params={'select':'updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio,daysum:record->ai->day_summary','opp_id':f'eq.{oid}'},headers=SH,verify=False,timeout=(10,60)).json()
    if not r: return None
    r=r[0]; ds=r.get('scores') or {}; hl=ds.get('headline') or {}; sv=(r.get('studio') or {}).get('versions') or {}; dsum=r.get('daysum') or {}
    return {'upd':r.get('updated_at'),'win':hl.get('win_position'),'mom':hl.get('deal_momentum'),'src':ds.get('factor_source'),'eng':sv.get('win'),'daysum':dsum.get('source'),'overall':(dsum.get('overall') or '')[:160]}
def model(oid,since):
    r=requests.get(f'{SB}/rest/v1/deal_trigger_runs',params={'select':'model,source,status','opp_id':f'eq.{oid}','created_at':f'gte.{since.isoformat()}','order':'created_at.desc','limit':'2'},headers=SH,verify=False,timeout=60).json()
    return r[0] if r else {}
base={}; c0=now()
for lbl,oid in DEALS:
    base[oid]=(rec(oid) or {}).get('upd')
    print(f"[{ts()}] enqueue {lbl}: {((requests.post(f'{API}/api/deal-engine/sweep/trigger',headers=AH,json={'opp_id':oid,'source':'manual'},verify=False,timeout=60).json() or {}).get('results') or {}).get(oid)}",flush=True)
    time.sleep(1)
done={}; t0=time.time()
while len(done)<len(DEALS) and time.time()-t0<2000:
    time.sleep(45)
    for lbl,oid in DEALS:
        if oid in done: continue
        a=rec(oid)
        if a and a['upd']!=base[oid] and a['win'] is not None:
            m=model(oid,c0)
            ok = a['src']=='ai' and str(a['eng'])=='10.8' and 'sonnet-5' in (m.get('model') or '')
            print(f"[{ts()}] RESTORED {lbl:14} win={a['win']} mom={a['mom']} v{a['eng']} src={a['src']} model={m.get('model')} summary={a['daysum']} {'OK' if ok else 'CHECK'}",flush=True)
            print(f"[{ts()}]    summary: {a['overall']}",flush=True)
            done[oid]=a
    if len(done)<len(DEALS): print(f"[{ts()}]  ... {len(done)}/{len(DEALS)} restored ({int(time.time()-t0)//60}m)",flush=True)
print('RESTORE-DAMAGE-DONE',flush=True)
