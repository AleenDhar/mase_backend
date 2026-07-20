import warnings, json; warnings.filterwarnings("ignore")
import requests, urllib3, boto3, botocore.config; urllib3.disable_warnings()
cfg = {}
for l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local", encoding="utf-8"):
    l = l.strip()
    if l and not l.startswith("#") and "=" in l:
        k, v = l.split("=", 1); cfg[k.strip()] = v.strip().strip('"').strip("'")
API = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
AH = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}"}
# 1) try to read a deployed git SHA / version from the API
for p in ("/health", "/version", "/api/health", "/api/version", "/"):
    try:
        r = requests.get(f"{API}{p}", headers=AH, verify=False, timeout=(8, 20))
        body = r.text[:300]
        if any(k in body.lower() for k in ("sha", "commit", "version", "git", "rev")):
            print(f"GET {p} -> {r.status_code}: {body}")
    except Exception:
        pass
# 2) ECS: current live api rev + whether a NEW deploy is rolling
BC = botocore.config.Config(connect_timeout=8, read_timeout=25, retries={"max_attempts": 2})
ecs = boto3.client("ecs", region_name="ap-south-1", verify=False, config=BC)
elb = boto3.client("elbv2", region_name="ap-south-1", verify=False, config=BC)
lbs = elb.describe_load_balancers()["LoadBalancers"]
alb = next((l for l in lbs if "mase-alb" in l["LoadBalancerName"]), lbs[0])
lst = elb.describe_listeners(LoadBalancerArn=alb["LoadBalancerArn"])["Listeners"]
http = next((x for x in lst if x["Port"] == 80), lst[0])
w = {t["TargetGroupArn"].split("/")[-2]: t.get("Weight", 0) for t in http["DefaultActions"][0].get("ForwardConfig", {}).get("TargetGroups", [])}
live = "mase-api-green" if w.get("mase-green", 0) >= w.get("mase-blue", 0) else "mase-api-blue"
print("ALB live weights:", w, "-> live colour:", live)
for svc in ("mase-api-blue", "mase-api-green"):
    s = ecs.describe_services(cluster="mase-cluster", services=[svc])["services"][0]
    for d in s["deployments"]:
        td = d["taskDefinition"].split("/")[-1]
        print(f"  {svc:16} td={td:16} status={d.get('status')} run={d['runningCount']}/{d['desiredCount']} roll={d.get('rolloutState')} updated={d.get('updatedAt').strftime('%H:%M:%S UTC') if d.get('updatedAt') else '?'}")
