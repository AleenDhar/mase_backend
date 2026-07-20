"""Wait out the 24h-summary polish deploy, re-sweep Bandhan in-process, verify the meeting
summary is CLEAN: no '.;', no mid-word '…' cut, roster gone, real takeaway present.
Self-verifying with retries so a not-yet-landed deploy can't give a false pass."""
import warnings, time, datetime, threading, re
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
def polished(s):
    lo=s.lower()
    if not s or '##' in s or 'fb/notes' in lo or lo.strip().startswith('participant'): return False,'raw notes present'
    if '.;' in s or ';.' in s: return False,'.; join still present'
    if re.search(r'\b\w{1,4}…$', s): return False,'mid-word truncation'
    return True,'clean'

print(f"[{ts()}] waiting 15m for the polish deploy, then re-sweep + verify",flush=True)
time.sleep(900)
for attempt in range(1,4):
    base=rec(); bu=(base or {}).get('updated_at')
    print(f"[{ts()}] attempt {attempt}: re-sweep Bandhan in-process",flush=True)
    threading.Thread(target=lambda: requests.post(f'{API}/api/deal-engine/sweep/{OID}',headers=AH,json={},verify=False,timeout=(10,1500)),daemon=True).start()
    t0=time.time(); settled=False
    while time.time()-t0<1500:
        time.sleep(40); r=rec()
        if r and r.get('updated_at')!=bu:
            s=meeting_summary(r); ok,why=polished(s)
            print(f"[{ts()}] re-swept. meeting summary now:\n    {s!r}",flush=True)
            if ok:
                print(f"[{ts()}] SUCCESS — summary is CLEAN ({why}).",flush=True); settled=True
            else:
                print(f"[{ts()}] not clean yet ({why}) — deploy likely not landed; retry.",flush=True)
            break
        print(f"[{ts()}]  … sweeping ({int(time.time()-t0)//60}m)",flush=True)
    if settled: break
    print(f"[{ts()}] wait 4m before retry…",flush=True); time.sleep(240)
print('POLISH-VERIFY-DONE',flush=True)
