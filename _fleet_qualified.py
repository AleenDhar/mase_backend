"""Re-sweep all 253 QUALIFIED opps FROM SCRATCH (update-living-memory mode) — /sweep/{oid}/
update-living-memory. Scales blue, conc 24. Resume-safe: appends done ids to a file + skips
them on relaunch. From-scratch skips the reconciler by design, so no retirement stats."""
import sys, time, json, os, warnings, datetime, csv
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
TASKS,CONC,PER_TO=30,24,2900
DONE="cc_work/_qualified_done.txt"
done_ids=set()
if os.path.exists(DONE):
    done_ids=set(x.strip() for x in open(DONE,encoding="utf-8") if x.strip())
ALL=[(d["lbl"], d["opp_id"]) for d in json.load(open("cc_work/_qualified_set.json",encoding="utf-8"))]
DEALS=[(l,o) for l,o in ALL if o not in done_ids]
SEL=("updated_at,eng:record->ai->scoring_studio->versions->win,"
     "w:record->ai->deal_scores->headline->win_position,m:record->ai->deal_scores->headline->deal_momentum")
done=[len(done_ids)]; RES={}
def ts(): return (datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=5,minutes=30)).strftime("%H:%M IST")
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
_dl=__import__("threading").Lock()
def mark(oid):
    with _dl:
        with open(DONE,"a",encoding="utf-8") as fh: fh.write(oid+"\n")
def one(lbl,oid):
    base=rec(oid); bu=base.get("updated_at")
    try: requests.post(f"{API}/api/deal-engine/sweep/{oid}/update-living-memory",headers=AH,json={},verify=False,timeout=(10,2500))
    except Exception: pass
    t0=time.time()
    while time.time()-t0<PER_TO:
        time.sleep(30)
        try: a=rec(oid)
        except Exception: continue
        if a.get("updated_at")!=bu and a.get("w") is not None:
            done[0]+=1; mark(oid); RES[oid]={"lbl":lbl,"ok":True,"a":a}
            print(f"[{ts()}] OK {done[0]}/{len(ALL)} {lbl[:26]:26} win={a.get('w')} mom={a.get('m')} v{a.get('eng')}",flush=True)
            return
    done[0]+=1; RES[oid]={"lbl":lbl,"ok":False,"a":rec(oid)}
    print(f"[{ts()}] TO {done[0]}/{len(ALL)} {lbl[:26]}",flush=True)
if __name__=="__main__":
    LIVE=live_colour()
    print(f"[{ts()}] QUALIFIED from-scratch: {len(DEALS)} to run ({len(done_ids)} already done) of {len(ALL)} | live={LIVE} scaling {TASKS}/conc {CONC}",flush=True)
    scale(LIVE,TASKS)
    with ThreadPoolExecutor(max_workers=CONC) as ex: list(ex.map(lambda d: one(*d), DEALS))
    print(f"[{ts()}] scaling {LIVE} back to 2",flush=True)
    try: scale(LIVE,2)
    except Exception as e: print(f"scaleback err {e}",flush=True)
    ok=sum(1 for r in RES.values() if r.get("ok"))
    with open("cc_work/_fleet_qualified.csv","w",newline="",encoding="utf-8-sig") as fh:
        w=csv.writer(fh); w.writerow(["deal","opp_id","ok","win","mom","eng"])
        for lbl,oid in ALL:
            r=RES.get(oid,{}); a=r.get("a") or {}
            w.writerow([lbl,oid,r.get("ok"),a.get("w"),a.get("m"),a.get("eng")])
    print(f"\n{ok}/{len(DEALS)} freshly swept from-scratch this run. FLEET-QUALIFIED-DONE",flush=True)
