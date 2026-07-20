import warnings; warnings.filterwarnings("ignore")
import boto3, botocore.config, urllib3; urllib3.disable_warnings()
BC = botocore.config.Config(connect_timeout=8, read_timeout=25, retries={"max_attempts": 2})
eb = boto3.client("events", region_name="ap-south-1", verify=False, config=BC)
# The partner event source behind the bus.
name = "aws.partner/salesforce.com/00D2000000016T9EAI/0YLP7000000arPBOAY"
try:
    src = eb.describe_event_source(Name=name)
    print("EVENT SOURCE state:", src.get("State"), "| created:", src.get("CreatedBy"), "| expires:", src.get("ExpirationTime"))
except Exception as e:
    print("describe_event_source:", type(e).__name__, str(e)[:200])
# list all partner sources visible
try:
    for s in eb.list_event_sources().get("EventSources", []):
        print("  source:", s.get("Name"), "->", s.get("State"))
except Exception as e:
    print("list_event_sources:", type(e).__name__, str(e)[:160])
# Is there a Pipe / relay object on our side?
try:
    pipes = boto3.client("pipes", region_name="ap-south-1", verify=False, config=BC).list_pipes().get("Pipes", [])
    for p in pipes:
        print("  pipe:", p.get("Name"), p.get("CurrentState"))
except Exception as e:
    print("pipes:", type(e).__name__, str(e)[:120])
