import sys, time, threading, warnings, datetime
from concurrent.futures import ThreadPoolExecutor
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
cfg={}
for l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local",encoding="utf-8"):
    l=l.strip()
    if l and not l.startswith("#") and "=" in l:
        k,v=l.split("=",1); cfg[k.strip()]=v.strip()
API=cfg["DEAL_ENGINE_API_BASE"].rstrip("/"); AH={"Authorization":"Bearer "+cfg["DEAL_ENGINE_TOKEN"],"Content-Type":"application/json"}
SB=cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/"); K=cfg["SUPABASE_SERVICE_ROLE_KEY"]; SH={"apikey":K,"Authorization":"Bearer "+K}
T=[("Robert Bosch","006P700000PlMpu"),("Mair Group","006P700000PtQGP"),("MTR","006P700000KTTO5")]
SEL="updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio,cov:record->evidence_coverage"
def ts(): return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
def st(o):
    r=requests.get(SB+"/rest/v1/deal_records",params={"select":SEL,"opp_id":"eq."+o},headers=SH,verify=False,timeout=(10,60)).json()
    if not r: return None
    r=r[0]; ds=r.get("scores") or {}; hl=ds.get("headline") or {}; sv=(r.get("studio") or {}).get("versions") or {}
    return {"upd":r.get("updated_at"),"win":hl.get("win_position"),"mom":hl.get("deal_momentum"),"read":hl.get("read"),"src":ds.get("factor_source"),"eng":sv.get("win"),"calls":(r.get("cov") or {}).get("calls_read")}
def run(lbl,o):
    b=(st(o) or {}).get("upd")
    print(f"[{ts()}] {lbl}: firing in-process",flush=True)
    try: requests.post(f"{API}/api/deal-engine/sweep/{o}",headers=AH,json={},verify=False,timeout=(10,2000))
    except Exception: pass
    t0=time.time()
    while time.time()-t0<3000:
        time.sleep(30)
        try: s=st(o)
        except Exception: continue
        if s and s["upd"]!=b and s["win"] is not None:
            ok=s["src"]=="ai" and str(s["eng"])=="10.8"
            print(f"[{ts()}] ✅ {lbl}: win={s['win']} mom={s['mom']} read={s['read']!r} eng=v{s['eng']} calls={s['calls']} {'GOVERNED' if ok else 'CHECK'}",flush=True); return
    print(f"[{ts()}] ❌ {lbl}: timeout 50m",flush=True)
with ThreadPoolExecutor(max_workers=3) as ex: list(ex.map(lambda d: run(*d), T))
print(f"[{ts()}] RESTORE3-DONE",flush=True)
