"""Poll ECS until the new deploy (task def rev > 297) is LIVE (ALB 100%) + COMPLETED, or timeout."""
import sys, time, warnings, datetime
warnings.filterwarnings("ignore")
import boto3, botocore.config, urllib3
urllib3.disable_warnings()
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
BC = botocore.config.Config(connect_timeout=10, read_timeout=35, retries={"max_attempts": 3})
ecs = boto3.client("ecs", region_name="ap-south-1", verify=False, config=BC)
elb = boto3.client("elbv2", region_name="ap-south-1", verify=False, config=BC)
BASE_REV = 308  # highest api rev before this deploy

def ts(): return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")

def live_colour_and_weights():
    lbs = elb.describe_load_balancers()["LoadBalancers"]
    alb = next((l for l in lbs if "mase-alb" in l["LoadBalancerName"]), lbs[0])
    lst = elb.describe_listeners(LoadBalancerArn=alb["LoadBalancerArn"])["Listeners"]
    http = next((x for x in lst if x["Port"] == 80), lst[0])
    tgs = http["DefaultActions"][0].get("ForwardConfig", {}).get("TargetGroups", [])
    w = {t["TargetGroupArn"].split("/")[-2]: t.get("Weight", 0) for t in tgs}
    live = "mase-api-green" if w.get("mase-green", 0) >= w.get("mase-blue", 0) else "mase-api-blue"
    return live, w

t0 = time.time()
while time.time() - t0 < 1800:  # 30 min cap
    try:
        live, w = live_colour_and_weights()
        rows = {}
        maxrev = 0
        for svc in ("mase-api-blue", "mase-api-green", "mase-worker"):
            s = ecs.describe_services(cluster="mase-cluster", services=[svc])["services"][0]
            d0 = s["deployments"][0]
            rev = int(d0["taskDefinition"].split(":")[-1])
            if svc.startswith("mase-api"):
                maxrev = max(maxrev, rev)
            rows[svc] = (rev, s["runningCount"], s["desiredCount"], d0.get("rolloutState"), len(s["deployments"]))
        live_rev, live_run, live_des, live_roll, live_ndep = rows[live]
        print(f"[{ts()}] live={live}(rev{live_rev},{live_run}/{live_des},{live_roll}) "
              f"maxApiRev={maxrev} weights={w} worker=rev{rows['mase-worker'][0]}", flush=True)
        # done: the LIVE colour is serving a rev > baseline, at steady state, single deployment
        if live_rev > BASE_REV and live_roll == "COMPLETED" and live_run == live_des and live_ndep == 1:
            print(f"[{ts()}] DEPLOY LIVE: {live} serving mase-api:{live_rev} (was <= {BASE_REV}). "
                  f"worker rev {rows['mase-worker'][0]}.", flush=True)
            print("DEPLOYWATCH-DONE", flush=True)
            break
    except Exception as e:
        print(f"[{ts()}] poll err {type(e).__name__}: {e}", flush=True)
    time.sleep(45)
else:
    print(f"[{ts()}] DEPLOYWATCH-TIMEOUT after 30min", flush=True)
