"""After the 24h-summary deploy lands, re-sweep Bandhan IN-PROCESS (bypasses the stale worker)
and verify the meeting item is now a clean summary, not the raw '## Participants' dump.
Self-verifying: if the first re-sweep still shows raw notes, the deploy hadn't landed -> retry."""
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
def rec():
    r=requests.get(f'{SB}/rest/v1/deal_records',params={'select':'updated_at,record','opp_id':f'eq.{OID}'},headers=SH,verify=False,timeout=90).json()
    return r[0] if r else None
def meeting_summary(r):
    ai=(r.get('record') or {}).get('ai') or {}
    for it in (ai.get('day_summary') or {}).get('items',[]):
        if it.get('kind')=='meeting': return it.get('summary') or ''
    return ''
def is_clean(s):
    lo=s.lower()
    return bool(s) and '##' not in s and 'fb/notes' not in lo and not lo.strip().startswith('participant')

INITIAL_WAIT=900   # ~15 min for the blue-green deploy
print(f"[{ts()}] waiting {INITIAL_WAIT//60}m for the deploy to land before re-sweeping…",flush=True)
time.sleep(INITIAL_WAIT)
for attempt in range(1,4):
    base=rec(); bu=(base or {}).get('updated_at')
    print(f"[{ts()}] attempt {attempt}: re-sweep Bandhan in-process",flush=True)
    threading.Thread(target=lambda: requests.post(f'{API}/api/deal-engine/sweep/{OID}',headers=AH,json={},verify=False,timeout=(10,1500)),daemon=True).start()
    t0=time.time(); done=False
    while time.time()-t0<1500:
        time.sleep(40); r=rec()
        if r and r.get('updated_at')!=bu:
            s=meeting_summary(r)
            print(f"[{ts()}] re-swept. meeting summary now:\n    {s[:260]!r}",flush=True)
            if is_clean(s):
                print(f"[{ts()}] SUCCESS — meeting summary is CLEAN (no raw roster/markdown).",flush=True); done=True
            else:
                print(f"[{ts()}] still raw — deploy likely not landed yet; will retry.",flush=True)
            break
        print(f"[{ts()}]  … sweeping ({int(time.time()-t0)//60}m)",flush=True)
    if done: break
    print(f"[{ts()}] waiting 4m before retry…",flush=True); time.sleep(240)
print('BANDHAN-RESWEEP-DONE',flush=True)
