"""Post-reconciler-deploy fleet (rev 298). Phase 1: re-sweep all 289 on the NEW keep-LM +
reconciler logic (/sweep/{oid}) -> v10.10, capturing reconciler retirements. Phase 2: re-run
the 23 null-win deals FROM SCRATCH (/sweep/{oid}/update-living-memory) to rebuild them clean.
Scales the LIVE colour (blue, rev298). Resumable: Phase-1 skips deals already v10.10 w/ a score."""
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

TASKS, CONC, PER_TO = 26, 22, 2700
NEW="10.10"
ALL=[(d.get("acct") or d["opp_id"], d["opp_id"]) for d in json.load(open("cc_work/_combined_set.json",encoding="utf-8"))]
N23=[(d.get("account_name") or d["opp_id"], d["opp_id"]) for d in json.load(open("cc_work/_null23.json",encoding="utf-8"))]
SEL=("updated_at,eng:record->ai->scoring_studio->versions->win,"
     "w:record->ai->deal_scores->headline->win_position,m:record->ai->deal_scores->headline->deal_momentum,"
     "pkts:record->packets")
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
def retn(pk):
    return [p for p in (pk or []) if p.get("status")=="resolved" and p.get("retire_evidence")]
def sweep(oid, path, skip_if_fresh):
    base=rec(oid); bu=base.get("updated_at")
    if skip_if_fresh and str(base.get("eng"))==NEW and base.get("w") is not None:
        return {"skip":True,"a":base,"ret":len(retn(base.get("pkts")))}
    try: requests.post(f"{API}/api/deal-engine/sweep/{oid}{path}",headers=AH,json={},verify=False,timeout=(10,1800))
    except Exception: pass
    t0=time.time()
    while time.time()-t0<PER_TO:
        time.sleep(30)
        try: a=rec(oid)
        except Exception: continue
        if a.get("updated_at")!=bu and a.get("w") is not None:
            return {"ok":str(a.get("eng"))==NEW,"a":a,"ret":len(retn(a.get("pkts")))}
    return {"timeout":True,"a":rec(oid),"ret":0}

def run_phase(name, deals, path, skip, RES):
    done=[0]
    def one(lbl,oid):
        r=sweep(oid,path,skip); RES[oid]={"lbl":lbl,**r}; done[0]+=1
        a=r.get("a") or {}
        tag=("SKIP" if r.get("skip") else "TO  " if r.get("timeout") else "OK  ")
        print(f"[{ts()}] {name} {tag} {done[0]}/{len(deals)} {lbl[:24]:24} win={a.get('w')} "
              f"mom={a.get('m')} v{a.get('eng')} retired={r.get('ret')}", flush=True)
    with ThreadPoolExecutor(max_workers=CONC) as ex:
        list(ex.map(lambda d: one(*d), deals))

if __name__=="__main__":
    LIVE=live_colour()
    print(f"[{ts()}] live={LIVE} scaling to {TASKS}. Phase1=289 keep-LM, Phase2=23 from-scratch", flush=True)
    scale(LIVE,TASKS)
    R1={}; print(f"[{ts()}] === PHASE 1: 289 on keep-LM+reconciler ===", flush=True)
    run_phase("P1", ALL, "", True, R1)
    R2={}; print(f"[{ts()}] === PHASE 2: 23 null-win FROM SCRATCH ===", flush=True)
    run_phase("P2", N23, "/update-living-memory", False, R2)
    print(f"[{ts()}] scaling {LIVE} back to 2", flush=True)
    try: scale(LIVE,2)
    except Exception as e: print(f"scaleback err {e}", flush=True)
    ok1=sum(1 for r in R1.values() if r.get("ok") or r.get("skip"))
    tot_ret=sum(r.get("ret") or 0 for r in R1.values())
    ok2=sum(1 for r in R2.values() if (r.get("a") or {}).get("w") is not None and not r.get("timeout"))
    with open("cc_work/_fleet_full_p1.csv","w",newline="",encoding="utf-8-sig") as fh:
        w=csv.writer(fh); w.writerow(["deal","opp_id","ok","win","mom","eng","retired"])
        for lbl,oid in ALL:
            r=R1.get(oid,{}); a=r.get("a") or {}
            w.writerow([lbl,oid,r.get("ok") or r.get("skip"),a.get("w"),a.get("m"),a.get("eng"),r.get("ret")])
    with open("cc_work/_fleet_full_p2.csv","w",newline="",encoding="utf-8-sig") as fh:
        w=csv.writer(fh); w.writerow(["deal","opp_id","win_fixed","win","eng"])
        for lbl,oid in N23:
            r=R2.get(oid,{}); a=r.get("a") or {}
            w.writerow([lbl,oid,a.get("w") is not None,a.get("w"),a.get("eng")])
    print(f"\nPHASE1: {ok1}/{len(ALL)} on v{NEW}, {tot_ret} total reconciler retirements across the book", flush=True)
    print(f"PHASE2: {ok2}/{len(N23)} null-win deals now have a score (from-scratch)", flush=True)
    print("FLEET-FULL-DONE", flush=True)
