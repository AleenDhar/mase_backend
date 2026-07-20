"""Read-only: verify the Supabase Management token works; list orgs + projects; find the
main project's region so the backup is provisioned in the same place. Prints no secrets."""
import sys, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

def load(path):
    d={}
    for l in open(path,encoding="utf-8", errors="ignore"):
        l=l.strip()
        if l and not l.startswith("#") and "=" in l:
            k,v=l.split("=",1); d[k.strip()]=v.strip().strip('"').strip("'")
    return d
sec=load(r"C:\Users\Aleen.Dhar\Downloads\Agent-Salesforce-Link (1)\Agent-Salesforce-Link\.supabase_secrets.env")
env=load(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local")
TOK=sec["SUPABASE_ACCESS_TOKEN"]
H={"Authorization":f"Bearer {TOK}"}
MAIN_URL=env.get("NEXT_PUBLIC_SUPABASE_URL","")
main_ref=MAIN_URL.split("//",1)[1].split(".")[0] if "//" in MAIN_URL else ""
print("main project ref:", main_ref)

r=requests.get("https://api.supabase.com/v1/organizations",headers=H,verify=False,timeout=30)
print("orgs:", r.status_code)
orgs=r.json() if r.status_code<300 else []
for o in orgs: print("  org:", o.get("id"), o.get("name"), "plan=", o.get("plan"))

r=requests.get("https://api.supabase.com/v1/projects",headers=H,verify=False,timeout=30)
print("projects:", r.status_code)
for p in (r.json() if r.status_code<300 else []):
    star=" <-- MAIN" if p.get("id")==main_ref else ""
    print(f"  {p.get('id')}  {str(p.get('name'))[:24]:26} region={p.get('region')} org={p.get('organization_id')} status={p.get('status')}{star}")
