"""Re-sweep the 253 LIVE deals (289 minus 36 dead) on the reconciler/keep-LM logic (rev 298),
/sweep/{oid} -> v10.10, capturing reconciler retirements. Scales blue. Resumable (skips v10.10)."""
import sys, time, json, warnings, datetime, csv
from concurrent.futures import ThreadPoolExecutor
warnings.filterwarnings("ignore")
import requests, urllib3, boto3, botocore.config
urllib3.disable_warnings()
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
cfg={}
for l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local",encoding="utf-8"):
    l=l.strip()
    if l and not l.startswith("#") and "=" in l:
        k,v=l.split("=",1); cfg[k.strip()]=v.strip().strip('"').strip("'")
API=cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
AH={"Authorization":f"Bearer {cfg['DEAL_ENGINE_TOKEN']}","Content-Type":"application/json"}
SB=cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/"); K=cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH={"apikey":K,"Authorization":f"Bearer {K}"}
BC=botocore.config.Config(connect_timeout=10,read_timeout=35,retries={"max_attempts":3})
ecs=boto3.client("ecs",region_name="ap-south-1",verify=False,config=BC)
elb=boto3.client("elbv2",region_name="ap-south-1",verify=False,config=BC)
TASKS,CONC,PER_TO=30,28,2700
NEW="10.10"
DEALS=[(d["lbl"], d["opp_id"]) for d in json.load(open("cc_work/_next_set.json",encoding="utf-8"))]
SEL=("updated_at,eng:record->ai->scoring_studio->versions->win,"
     "w:record->ai->deal_scores->headline->win_position,m:record->ai->deal_scores->headline->deal_momentum,"
     "pkts:record->packets")
done=[0]; RES={}
def ts(): return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
def live_colour():
    lbs=elb.describe_load_balancers()["LoadBalancers"]
    alb=next((l for l in lbs if "mase-alb" in l["LoadBalancerName"]),lbs[0])
    lst=elb.describe_listeners(LoadBalancerArn=alb["LoadBalancerArn"])["Listeners"]
    http=next((x for x in lst if x["Port"]==80),lst[0])
    tgs=http["DefaultActions"][0].get("ForwardConfig",{}).get("TargetGroups",[])
    w={t["TargetGroupArn"].split("/")[-2]:t.get("Weight",0) for t in tgs}
    return "mase-api-green" if w.get("mase-green",0)>=w.get("mase-blue",0) else "mase-api-blue"
def rec(oid):
    return (requests.get(f"{SB}/rest/v1/deal_records",params={"select":SEL,"opp_id":f"eq.{oid}"},headers=SH,verify=False,timeout=(10,90)).json() or [{}])[0]
def scale(svc,n):
    ecs.update_service(cluster="mase-cluster",service=svc,desiredCount=n)
    t0=time.time()
    while time.time()-t0<420:
        s=ecs.describe_services(cluster="mase-cluster",services=[svc])["services"][0]
        if s["runningCount"]>=n and s["deployments"][0].get("rolloutState")=="COMPLETED": return
        time.sleep(15)
def nret(pk): return [p for p in (pk or []) if p.get("status")=="resolved" and p.get("retire_evidence")]
def one(lbl,oid):
    base=rec(oid); bu=base.get("updated_at")
    if str(base.get("eng"))==NEW and base.get("w") is not None:
        done[0]+=1; RES[oid]={"lbl":lbl,"skip":True,"a":base,"ret":len(nret(base.get("pkts")))}
        print(f"[{ts()}] SKIP {done[0]}/{len(DEALS)} {lbl[:26]:26} v{NEW}",flush=True); return
    try: requests.post(f"{API}/api/deal-engine/sweep/{oid}",headers=AH,json={},verify=False,timeout=(10,1800))
    except Exception: pass
    t0=time.time()
    while time.time()-t0<PER_TO:
        time.sleep(30)
        try: a=rec(oid)
        except Exception: continue
        if a.get("updated_at")!=bu and a.get("w") is not None:
            r=nret(a.get("pkts")); done[0]+=1
            RES[oid]={"lbl":lbl,"ok":str(a.get("eng"))==NEW,"a":a,"ret":len(r),"rsamp":r[:2]}
            print(f"[{ts()}] OK  {done[0]}/{len(DEALS)} {lbl[:26]:26} win={a.get('w')} mom={a.get('m')} "
                  f"v{a.get('eng')} retired={len(r)}",flush=True)
            return
    done[0]+=1; RES[oid]={"lbl":lbl,"ok":False,"a":rec(oid),"ret":0}
    print(f"[{ts()}] TO  {done[0]}/{len(DEALS)} {lbl[:26]}",flush=True)
if __name__=="__main__":
    LIVE=live_colour()
    print(f"[{ts()}] {len(DEALS)} forecasted+Woodcock deals | live={LIVE} scaling to {TASKS} conc {CONC}",flush=True)
    scale(LIVE,TASKS)
    with ThreadPoolExecutor(max_workers=CONC) as ex: list(ex.map(lambda d: one(*d), DEALS))
    print(f"[{ts()}] scaling {LIVE} back to 2",flush=True)
    try: scale(LIVE,2)
    except Exception as e: print(f"scaleback err {e}",flush=True)
    ok=sum(1 for r in RES.values() if r.get("ok") or r.get("skip"))
    tot=sum(r.get("ret") or 0 for r in RES.values())
    withret=[(r["lbl"],r["ret"]) for r in RES.values() if (r.get("ret") or 0)>0]
    with open("cc_work/_fleet_next.csv","w",newline="",encoding="utf-8-sig") as fh:
        w=csv.writer(fh); w.writerow(["deal","opp_id","ok","win","mom","eng","retired"])
        for lbl,oid in DEALS:
            r=RES.get(oid,{}); a=r.get("a") or {}
            w.writerow([lbl,oid,r.get("ok") or r.get("skip"),a.get("w"),a.get("m"),a.get("eng"),r.get("ret")])
    print(f"\n{ok}/{len(DEALS)} on v{NEW}. Reconciler retired {tot} stale items across {len(withret)} deals.",flush=True)
    for lbl,n in sorted(withret,key=lambda t:-t[1])[:15]: print(f"   {lbl[:34]:34} retired {n}",flush=True)
    print("FLEET-NEXT-DONE",flush=True)
