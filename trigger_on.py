"""Turn the SFDC event trigger ON (reversible):
  1. Lambda mase-sf-cdc-bridge: CDC_TRIGGER_ON_ACTIVITY=true  (instant)
  2. mase-api-green (LIVE) + mase-worker: DEAL_SWEEP_MANUAL_ONLY=false
     (new task-def revision + rolling service update). Prints the OLD revision
     for instant rollback.
Only flips the flag env var; image + all other env unchanged."""
import sys, time, warnings, json
warnings.filterwarnings("ignore")
import boto3, botocore.config
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
BC = botocore.config.Config(connect_timeout=10, read_timeout=40, retries={"max_attempts": 2})
ecs = boto3.client("ecs", region_name="ap-south-1", verify=False, config=BC)
lam = boto3.client("lambda", region_name="ap-south-1", verify=False, config=BC)

RO = {"taskDefinitionArn", "revision", "status", "requiresAttributes", "compatibilities",
      "registeredAt", "registeredBy", "deregisteredAt"}


def flip_service(svc, name, value):
    s = ecs.describe_services(cluster="mase-cluster", services=[svc])["services"][0]
    old_arn = s["taskDefinition"]
    td = ecs.describe_task_definition(taskDefinition=old_arn)["taskDefinition"]
    # set the env var on every container
    for c in td.get("containerDefinitions", []):
        env = c.setdefault("environment", [])
        env[:] = [e for e in env if e.get("name") != name]
        env.append({"name": name, "value": value})
    reg = {k: v for k, v in td.items() if k not in RO}
    new = ecs.register_task_definition(**reg)["taskDefinition"]
    new_arn = new["taskDefinitionArn"]
    print(f"[{svc}] registered {new_arn.split('/')[-1]} (was {old_arn.split('/')[-1]}) — {name}={value}", flush=True)
    ecs.update_service(cluster="mase-cluster", service=svc, taskDefinition=new_arn)
    print(f"[{svc}] ROLLBACK: aws ecs update-service --cluster mase-cluster --service {svc} --task-definition {old_arn.split('/')[-1]}", flush=True)
    return old_arn, new_arn


def wait_rollout(svc):
    t0 = time.time()
    while time.time() - t0 < 300:
        s = ecs.describe_services(cluster="mase-cluster", services=[svc])["services"][0]
        d = s["deployments"][0]
        if d.get("rolloutState") == "COMPLETED" and s["runningCount"] >= s["desiredCount"]:
            print(f"[{svc}] rollout COMPLETED running={s['runningCount']}", flush=True)
            return True
        time.sleep(15)
    print(f"[{svc}] rollout wait timed out", flush=True)
    return False


# 1) Lambda activity flag — instant, reversible
try:
    fn = "mase-sf-cdc-bridge"
    cfg = lam.get_function_configuration(FunctionName=fn)
    env = (cfg.get("Environment") or {}).get("Variables") or {}
    prev = env.get("CDC_TRIGGER_ON_ACTIVITY", "(unset)")
    env["CDC_TRIGGER_ON_ACTIVITY"] = "true"
    lam.update_function_configuration(FunctionName=fn, Environment={"Variables": env})
    print(f"[lambda {fn}] CDC_TRIGGER_ON_ACTIVITY {prev} -> true (rollback: set back to unset/false)", flush=True)
except Exception as e:  # noqa: BLE001
    print(f"[lambda] flip failed: {type(e).__name__}: {e}", flush=True)

# 2) manual_only=false on the LIVE api + worker
oldg, _ = flip_service("mase-api-green", "DEAL_SWEEP_MANUAL_ONLY", "false")
if wait_rollout("mase-api-green"):
    oldw, _ = flip_service("mase-worker", "DEAL_SWEEP_MANUAL_ONLY", "false")
    wait_rollout("mase-worker")
else:
    print("green rollout not confirmed — NOT flipping worker; investigate before proceeding", flush=True)

print("\nTRIGGER-ON-DONE — SFDC trigger is now live (deal changes + next steps + activities).")
print("Watch: deal_trigger_runs (Supabase) or CloudWatch [DEAL-SWEEP] trigger logs.")
