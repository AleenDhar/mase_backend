"""Sequential, worker-proof finish. concurrency 1, verify each write before the next.
Step 0: mark any active sweep_queue row for our opps FAILED so the stale worker starves.
Step 1: for each opp not on clean v10.8, fire in-process, wait for a GOOD write, re-verify
        30s later that it wasn't re-clobbered. Never fire the next until the current is solid.
"""
import sys, time, warnings, datetime
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
def ts(): return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")

ALL=[("MTR","006P700000KTTO5"),("Gamuda","006P700000Q15OU"),("Cebu","0066700000wdNe1"),
     ("Bandhan","006P700000H55TV"),("Mair","006P700000PtQGP"),
     ("Bosch","006P700000PlMpu"),("Temasek","006P700000BV2eA"),("Techtronic","006P700000GWfrf"),
     ("SAMI","006P700000RD9Ir"),("Wheelson","006P700000VlPdp"),("Khansaheb","006P700000LtIUv"),
     ("HAECO","006P700000NwbBd"),("Globe","006P7000008hZHF"),("Arabian","006P700000QvP7Z"),
     ("ACEN","006P700000DkWgX")]
SEL="updated_at,scores:record->ai->deal_scores,studio:record->ai->scoring_studio,cov:record->evidence_coverage"
def stt(o):
    r=requests.get(SB+"/rest/v1/deal_records",params={"select":SEL,"opp_id":"eq."+o},headers=SH,verify=False,timeout=(10,60)).json()
    if not r: return None
    r=r[0]; ds=r.get("scores") or {}; hl=ds.get("headline") or {}; sv=(r.get("studio") or {}).get("versions") or {}
    return {"upd":r.get("updated_at"),"win":hl.get("win_position"),"mom":hl.get("deal_momentum"),
            "read":hl.get("read"),"src":ds.get("factor_source"),"eng":sv.get("win"),"calls":(r.get("cov") or {}).get("calls_read")}
def good(s): return s and s["win"] is not None and s["src"]=="ai" and str(s["eng"])=="10.8"

# STEP 0 — starve the worker
IN="("+",".join(o for _,o in ALL)+")"
q=requests.get(SB+"/rest/v1/sweep_queue",params={"select":"opp_id,account_name,status","opp_id":"in."+IN,"status":"in.(waiting,working)"},headers=SH,verify=False,timeout=60).json()
print(f"[{ts()}] active queue rows to halt: {len(q)}",flush=True)
for x in q:
    requests.patch(SB+"/rest/v1/sweep_queue",params={"opp_id":"eq."+x["opp_id"]},
                   headers={**SH,"Content-Type":"application/json","Prefer":"return=minimal"},
                   json={"status":"failed","error":"operator halt: stale worker starve 2026-07-09"},verify=False,timeout=60)
    print(f"[{ts()}]   halted {x.get('account_name')}",flush=True)

# STEP 1 — sequential finish
todo=[(lbl,o) for lbl,o in ALL if not good(stt(o))]
print(f"[{ts()}] need clean v10.8: {[l for l,_ in todo]}\n",flush=True)
for lbl,o in todo:
    for attempt in (1,2):
        print(f"[{ts()}] {lbl}: fire (attempt {attempt})",flush=True)
        b=(stt(o) or {}).get("upd")
        try: requests.post(f"{API}/api/deal-engine/sweep/{o}",headers=AH,json={},verify=False,timeout=(10,2400))
        except Exception: pass
        t0=time.time(); s=None
        while time.time()-t0<1800:
            time.sleep(30); 
            try: s=stt(o)
            except Exception: continue
            if s and s["upd"]!=b and s["win"] is not None: break
        if good(s):
            time.sleep(35); s2=stt(o)   # re-verify no clobber
            if good(s2):
                print(f"[{ts()}] OK {lbl}: win={s2['win']} mom={s2['mom']} read={s2['read']!r} calls={s2['calls']} (stable)",flush=True); break
            print(f"[{ts()}] {lbl} re-clobbered after write -> retry",flush=True)
        else:
            print(f"[{ts()}] {lbl}: bad/no write ({s}) -> retry",flush=True)
    else:
        print(f"[{ts()}] GIVE UP {lbl} after 2 attempts",flush=True)
print(f"[{ts()}] FINISH-DONE",flush=True)
