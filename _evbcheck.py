import warnings; warnings.filterwarnings("ignore")
import boto3, botocore.config, urllib3; urllib3.disable_warnings()
BC = botocore.config.Config(connect_timeout=8, read_timeout=25, retries={"max_attempts": 2})
eb = boto3.client("events", region_name="ap-south-1", verify=False, config=BC)
# list all event buses (default + partner/salesforce)
buses = eb.list_event_buses()["EventBuses"]
print("=== EVENT BUSES ===")
for b in buses:
    print("  ", b["Name"])
print("=== RULES targeting the cdc-bridge lambda, per bus ===")
for b in buses:
    bus = b["Name"]
    try:
        rules = eb.list_rules(EventBusName=bus).get("Rules", [])
    except Exception as e:
        print(f"  [{bus}] list_rules err: {e}"); continue
    for r in rules:
        try:
            tg = eb.list_targets_by_rule(Rule=r["Name"], EventBusName=bus).get("Targets", [])
        except Exception:
            tg = []
        arns = [t.get("Arn", "") for t in tg]
        if any("cdc-bridge" in a or "mase-sf-cdc" in a for a in arns):
            print(f"  BUS={bus} RULE={r['Name']} STATE={r.get('State')} "
                  f"-> targets={[a.split(':')[-1] for a in arns]}")
            if r.get("EventPattern"):
                print("      pattern:", str(r.get('EventPattern'))[:300])
