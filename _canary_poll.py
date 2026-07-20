import sys, time, warnings, datetime
warnings.filterwarnings("ignore")
import requests, urllib3; urllib3.disable_warnings()
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
cfg={}
for l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local",encoding="utf-8"):
    l=l.strip()
    if l and not l.startswith("#") and "=" in l:
        k,v=l.split("=",1); cfg[k.strip()]=v.strip().strip('"').strip("'")
SB=cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/"); K=cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH={"apikey":K,"Authorization":f"Bearer {K}"}
OID="006P700000X6W3q"
SEL=("updated_at,eng:record->ai->scoring_studio->versions->win,"
     "w:record->ai->deal_scores->headline->win_position,m:record->ai->deal_scores->headline->deal_momentum,"
     "rd:record->ai->deal_scores->headline->read,pkts:record->packets")
def ts(): return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
def rec():
    return (requests.get(f"{SB}/rest/v1/deal_records",params={"select":SEL,"opp_id":f"eq.{OID}"},headers=SH,verify=False,timeout=(10,90)).json() or [{}])[0]
prev=rec()
print(f"[{ts()}] polling Birmingham (currently v{prev.get('eng')} win={prev.get('w')} updated={prev.get('updated_at')})", flush=True)
t0=time.time()
while time.time()-t0<2400:
    time.sleep(30)
    try: a=rec()
    except Exception: continue
    # fresh = updated within this run window AND v10.10 (new code) OR updated_at advanced past prev
    if a.get("updated_at")!=prev.get("updated_at") and a.get("w") is not None:
        pk=a.get("pkts") or []
        act=[p for p in pk if str(p.get("status") or "active")=="active"]
        res=[p for p in pk if p.get("status")=="resolved"]
        retn=[p for p in res if p.get("retire_evidence")]
        req=len([p for p in act if p.get("type")=="requirement"]); com=len([p for p in act if p.get("type")=="commitment"])
        print(f"[{ts()}] DONE win={a.get('w')} mom={a.get('m')} read={a.get('rd')} v{a.get('eng')} "
              f"| pkts:{len(act)}act/{len(res)}res req={req} com={com} retired_now={len(retn)}", flush=True)
        for rp in retn[:6]:
            v=rp.get("value") if isinstance(rp.get("value"),dict) else {}
            txt=rp.get("subject") or v.get("value") or v.get("requirement") or v.get("deliverable") or "?"
            print(f"   RETIRED[{rp.get('type')}] {str(txt)[:75]!r} <- {str(rp.get('retire_evidence'))[:100]!r}", flush=True)
        print("CANARY-DONE-v10.10" if str(a.get("eng"))=="10.10" else f"CANARY-DONE-CHECK-eng={a.get('eng')}", flush=True)
        break
else:
    print(f"[{ts()}] CANARY-POLL-TIMEOUT", flush=True)
