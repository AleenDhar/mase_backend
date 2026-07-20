"""Finish the screenshot set on v10.8 via the SAFE in-process endpoint (correct api image).
POST /api/deal-engine/sweep/{oid} -> analyze_one in-process. Never touches the stale worker.
5 concurrent (4GB api headroom). ACEN is a guard: must stay ~20 (dead-deal momentum gate)."""
import sys, time, threading, datetime
from concurrent.futures import ThreadPoolExecutor
import warnings; warnings.filterwarnings("ignore")
import requests, urllib3; urllib3.disable_warnings()
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
cfg={}
for l in open(r'C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local',encoding='utf-8'):
    l=l.strip()
    if l and not l.startswith('#') and '=' in l:
        k,v=l.split('=',1); cfg[k.strip()]=v.strip().strip('"').strip("'")
API=cfg['DEAL_ENGINE_API_BASE'].rstrip('/'); AH={'Authorization':'Bearer '+cfg['DEAL_ENGINE_TOKEN'],'Content-Type':'application/json'}
SB=cfg['NEXT_PUBLIC_SUPABASE_URL'].rstrip('/'); K=cfg['SUPABASE_SERVICE_ROLE_KEY']; SH={'apikey':K,'Authorization':'Bearer '+K}
DEALS=[("Mair Group","006P700000PtQGP"),("Gamuda","006P700000Q15OU"),("Bandhan Bank","006P700000H55TV"),
       ("Arabian Industries","006P700000QvP7Z"),("Cebu Pacific","0066700000wdNe1"),("ACEN","006P700000DkWgX")]
SEL="updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio,cov:record->evidence_coverage"
_lk=threading.Lock()
def ts(): return datetime.datetime.now(datetime.timezone.utc).strftime('%H:%M:%S')
def say(m):
    with _lk: print(f'[{ts()}] {m}',flush=True)
def state(oid):
    r=requests.get(f'{SB}/rest/v1/deal_records',params={'select':SEL,'opp_id':f'eq.{oid}'},headers=SH,verify=False,timeout=(10,60)).json()
    if not r: return None
    r=r[0]; ds=r.get('scores') or {}; hl=ds.get('headline') or {}; sv=(r.get('studio') or {}).get('versions') or {}
    return {'upd':r.get('updated_at'),'win':hl.get('win_position'),'mom':hl.get('deal_momentum'),'read':hl.get('read'),
            'src':ds.get('factor_source'),'engine':sv.get('win'),'deg':ds.get('scoring_degraded'),'calls':(r.get('cov') or {}).get('calls_read')}
def run(lbl,oid):
    b=state(oid); bu=(b or {}).get('upd')
    say(f'-> {lbl:20} start (was win={(b or {}).get("win")} mom={(b or {}).get("mom")} v{(b or {}).get("engine")})')
    try: requests.post(f'{API}/api/deal-engine/sweep/{oid}',headers=AH,json={},verify=False,timeout=(10,1500))
    except Exception: pass
    t0=time.time()
    while time.time()-t0<2100:
        time.sleep(35); a=state(oid)
        if a and a['upd']!=bu and a['win'] is not None:
            ok=a['src']=='ai' and not a['deg'] and str(a['engine'])=='10.8'
            say(f'OK {lbl:20} win={a["win"]} mom={a["mom"]} read={a["read"]!r} v{a["engine"]} calls={a["calls"]} {"GOVERNED" if ok else "CHECK"}')
            return
    say(f'TIMEOUT {lbl}')
say(f'in-process finish: {len(DEALS)} deals, 5 concurrent, SAFE path (no worker)')
with ThreadPoolExecutor(max_workers=5) as ex: list(ex.map(lambda d: run(*d),DEALS))
say('INPROCESS-FINISH-DONE')
