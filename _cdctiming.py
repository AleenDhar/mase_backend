import warnings, time, datetime; warnings.filterwarnings("ignore")
import boto3, botocore.config, urllib3; urllib3.disable_warnings()
BC = botocore.config.Config(connect_timeout=10, read_timeout=45, retries={"max_attempts": 2})
logs = boto3.client("logs", region_name="ap-south-1", verify=False, config=BC)
lg = "/aws/lambda/mase-sf-cdc-bridge"
now = int(time.time() * 1000)
ev = logs.filter_log_events(logGroupName=lg, startTime=now - 10 * 86400 * 1000, endTime=now, limit=2000)["events"]
def ist(ms): return (datetime.datetime.utcfromtimestamp(ms / 1000) + datetime.timedelta(hours=5, minutes=30)).strftime("%m-%d %H:%M IST")
inv = [e for e in ev if "[event]" in e["message"] and "detail-type" in e["message"]]
trg = [e for e in ev if "[trigger]" in e["message"]]
print("total log lines (10d):", len(ev), "| invocations:", len(inv), "| trigger posts:", len(trg))
print("--- last 12 invocations (Lambda received an SF event) ---")
for e in inv[-12:]:
    print("  ", ist(e["timestamp"]), e["message"].strip()[:80])
print("--- last 8 trigger posts ---")
for e in trg[-8:]:
    print("  ", ist(e["timestamp"]), e["message"].strip()[:90])
if inv:
    print("MOST RECENT SF event received:", ist(inv[-1]["timestamp"]))
