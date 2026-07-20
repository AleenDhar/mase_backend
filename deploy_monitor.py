"""Watch the prod deploy (triggered by push to origin/main) via AWS: CodeBuild build
status + ECS blue-green rollout (new task-def revision healthy). Exits when the deploy
completes (new revision live) or fails/rolls back."""
import sys, time, warnings, datetime
warnings.filterwarnings("ignore")
import boto3, botocore.config
BC = botocore.config.Config(connect_timeout=10, read_timeout=40, retries={"max_attempts": 2})
ecs = boto3.client("ecs", region_name="ap-south-1", verify=False, config=BC)
elb = boto3.client("elbv2", region_name="ap-south-1", verify=False, config=BC)
cb = boto3.client("codebuild", region_name="ap-south-1", verify=False, config=BC)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def ts():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")


def rev(td):
    return td.split(":")[-1] if td else "?"


def live_colour():
    lbs = elb.describe_load_balancers()["LoadBalancers"]
    alb = next((l for l in lbs if "mase-alb" in l["LoadBalancerName"]), lbs[0])
    lst = elb.describe_listeners(LoadBalancerArn=alb["LoadBalancerArn"])["Listeners"]
    http = next((x for x in lst if x["Port"] == 80), lst[0])
    tgs = http["DefaultActions"][0].get("ForwardConfig", {}).get("TargetGroups", [])
    live_tg = max(tgs, key=lambda t: t.get("Weight", 0))["TargetGroupArn"] if tgs else None
    for svc in ("mase-api-blue", "mase-api-green"):
        s = ecs.describe_services(cluster="mase-cluster", services=[svc])["services"][0]
        for lb in s.get("loadBalancers", []):
            if lb.get("targetGroupArn") == live_tg:
                return svc, rev(s["taskDefinition"])
    return "?", "?"


base_c, base_rev = live_colour()
print(f"[{ts()}] baseline: live={base_c} task-def rev={base_rev} (waiting for a new rev to go live)", flush=True)

t0 = time.time()
last = ""
while time.time() - t0 < 1800:
    time.sleep(30)
    line = f"[{ts()}] "
    # CodeBuild
    try:
        ids = cb.list_builds_for_project(projectName="mase-build", sortOrder="DESCENDING")["ids"][:1]
        if ids:
            b = cb.batch_get_builds(ids=ids)["builds"][0]
            line += f"codebuild={b.get('buildStatus')} phase={b.get('currentPhase')} | "
    except Exception as e:
        line += f"codebuild=? ({type(e).__name__}) | "
    # ECS both colours
    try:
        for svc in ("mase-api-blue", "mase-api-green"):
            s = ecs.describe_services(cluster="mase-cluster", services=[svc])["services"][0]
            d0 = s["deployments"][0]
            line += (f"{svc.split('-')[-1]}:rev{rev(s['taskDefinition'])} "
                     f"{d0.get('rolloutState','?')} {s['runningCount']}/{s['desiredCount']} | ")
    except Exception as e:
        line += f"ecs=? ({type(e).__name__})"
    if line != last:
        print(line, flush=True); last = line
    # Done? a new rev is live on the ALB colour + rollout complete
    try:
        c, r = live_colour()
        s = ecs.describe_services(cluster="mase-cluster", services=[c])["services"][0]
        if r != base_rev and s["deployments"][0].get("rolloutState") == "COMPLETED" \
                and s["runningCount"] >= s["desiredCount"] >= 1:
            print(f"[{ts()}] ✅ DEPLOY LIVE — {c} now serving task-def rev {r} (was {base_rev})", flush=True)
            break
    except Exception:
        pass
else:
    print(f"[{ts()}] ⏱ monitor timed out (deploy may still be running)", flush=True)
print("DEPLOY-MONITOR-DONE")
