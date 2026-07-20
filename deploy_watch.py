"""Poll ECS until the new mase-api revision (>=280) is live + healthy (deploy landed)."""
import subprocess, json, time, os
AWS = r"C:\Program Files\Amazon\AWSCLIV2\aws.exe"
ENV = {**os.environ, "AWS_CA_BUNDLE": r"C:\Users\Aleen.Dhar\.aws\corp-ca-bundle.pem"}
TARGET = 283  # baseline live was 282; the parenthetical-dedup deploy registers >= 283


def rev(td):
    try:
        return int(str(td).split(":")[-1])
    except Exception:
        return 0


stable = 0
for i in range(34):
    try:
        out = subprocess.run(
            [AWS, "ecs", "describe-services", "--cluster", "mase-cluster",
             "--services", "mase-api-blue", "mase-api-green", "--region", "ap-south-1",
             "--query", "services[].{n:serviceName,td:taskDefinition,run:runningCount,des:desiredCount,ro:deployments[0].rolloutState}",
             "--output", "json"], capture_output=True, text=True, env=ENV, timeout=60)
        svcs = json.loads(out.stdout or "[]")
    except Exception as e:
        print(f"[{i:02d}] poll error: {str(e)[:60]}", flush=True)
        time.sleep(45)
        continue
    line = " | ".join(f"{s['n'].split('-')[-1]}:td{rev(s['td'])} {s['run']}/{s['des']} {s['ro']}" for s in svcs)
    live_new = [s for s in svcs if rev(s["td"]) >= TARGET and (s.get("run") or 0) >= 1
                and s.get("ro") == "COMPLETED"]
    print(f"[{i:02d}] {line}  -> new_live={bool(live_new)} stable={stable}", flush=True)
    if live_new:
        stable += 1
        if stable >= 2:
            print(f"DEPLOY LANDED — mase-api td {rev(live_new[0]['td'])} live + healthy", flush=True)
            break
    else:
        stable = 0
    time.sleep(45)
else:
    print("deploy watch timed out (~25m) — check ECS manually", flush=True)
