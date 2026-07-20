"""Provision the dedicated 'mase-backup' Supabase project via the Management API.
Same org + region as main. Saves creds to .backup_secrets.env (gitignored). Prints no secrets."""
import sys, time, secrets, json, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
def load(p):
    d={}
    for l in open(p,encoding="utf-8",errors="ignore"):
        l=l.strip()
        if l and not l.startswith("#") and "=" in l:
            k,v=l.split("=",1); d[k.strip()]=v.strip().strip('"').strip("'")
    return d
sec=load(r"C:\Users\Aleen.Dhar\Downloads\Agent-Salesforce-Link (1)\Agent-Salesforce-Link\.supabase_secrets.env")
TOK=sec["SUPABASE_ACCESS_TOKEN"]; H={"Authorization":f"Bearer {TOK}","Content-Type":"application/json"}
ORG="tbcnzfagujbqiobaempj"; REGION="ap-south-1"; NAME="mase-backup"

# idempotent: reuse if a mase-backup project already exists
existing=requests.get("https://api.supabase.com/v1/projects",headers=H,verify=False,timeout=30).json()
proj=next((p for p in existing if p.get("name")==NAME),None)
if proj:
    ref=proj["id"]; print(f"reusing existing {NAME} ref={ref} status={proj.get('status')}")
    DBPASS="(unchanged — existing project)"
else:
    DBPASS=secrets.token_urlsafe(24)
    body={"name":NAME,"organization_id":ORG,"region":REGION,"db_pass":DBPASS}
    r=requests.post("https://api.supabase.com/v1/projects",headers=H,json=body,verify=False,timeout=60)
    print("create:",r.status_code)
    if r.status_code>=300:
        print(r.text[:400]); sys.exit(1)
    ref=r.json()["id"]; print(f"created {NAME} ref={ref}")

# poll until healthy
for i in range(40):
    p=requests.get(f"https://api.supabase.com/v1/projects/{ref}",headers=H,verify=False,timeout=30).json()
    st=p.get("status")
    print(f"  [{i}] status={st}",flush=True)
    if st=="ACTIVE_HEALTHY": break
    time.sleep(15)

# fetch service_role key
keys=requests.get(f"https://api.supabase.com/v1/projects/{ref}/api-keys",headers=H,verify=False,timeout=30).json()
svc=next((k["api_key"] for k in keys if k.get("name")=="service_role"),None)
anon=next((k["api_key"] for k in keys if k.get("name")=="anon"),None)
URL=f"https://{ref}.supabase.co"
out=(f"# mase-backup Supabase project (provisioned {NAME})\n"
     f"BACKUP_REF={ref}\nBACKUP_URL={URL}\n"
     f"BACKUP_SERVICE_KEY={svc}\nBACKUP_ANON_KEY={anon}\n")
if DBPASS!="(unchanged — existing project)":
    out+=f"BACKUP_DB_PASS={DBPASS}\n"
open(r"C:\Users\Aleen.Dhar\Downloads\Agent-Salesforce-Link (1)\Agent-Salesforce-Link\.backup_secrets.env","w").write(out)
print(f"\nBACKUP READY ref={ref} url={URL} service_key={'yes' if svc else 'MISSING'}")
print("wrote .backup_secrets.env (gitignored)")
