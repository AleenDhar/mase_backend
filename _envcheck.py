import warnings; warnings.filterwarnings("ignore")
import boto3, botocore.config, urllib3; urllib3.disable_warnings()
BC = botocore.config.Config(connect_timeout=8, read_timeout=25, retries={"max_attempts": 2})
ecs = boto3.client("ecs", region_name="ap-south-1", verify=False, config=BC)
for svc in ("mase-api-blue", "mase-worker"):
    s = ecs.describe_services(cluster="mase-cluster", services=[svc])["services"][0]
    td = s["deployments"][0]["taskDefinition"]
    d = ecs.describe_task_definition(taskDefinition=td)["taskDefinition"]
    env = {}
    for c in d["containerDefinitions"]:
        for e in (c.get("environment") or []):
            env[e["name"]] = e["value"]
    print("###", svc, td.split("/")[-1])
    for k in ["DEAL_SWEEP_MANUAL_ONLY", "DEAL_SWEEP_KEEP_LIVING_MEMORY", "DEAL_SWEEP_TRIGGER_COOLDOWN_HOURS", "SWEEP_AUTOSCALE_ENABLED"]:
        print("   ", k, "=", env.get(k, "<unset>"))
