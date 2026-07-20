"""Read-only: main project's public tables, live row counts, and on-disk sizes (via the
Management API query endpoint). Establishes the backup scope + a verification baseline."""
import sys, warnings
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
env=load(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local")
TOK=sec["SUPABASE_ACCESS_TOKEN"]; H={"Authorization":f"Bearer {TOK}","Content-Type":"application/json"}
MAIN=env["NEXT_PUBLIC_SUPABASE_URL"].split("//",1)[1].split(".")[0]
q=("select relname as t, n_live_tup as rows, "
   "pg_size_pretty(pg_total_relation_size(relid)) as size, pg_total_relation_size(relid) as bytes "
   "from pg_stat_user_tables where schemaname='public' order by pg_total_relation_size(relid) desc")
r=requests.post(f"https://api.supabase.com/v1/projects/{MAIN}/database/query",
                headers=H,json={"query":q},verify=False,timeout=60)
print("query status:", r.status_code)
if r.status_code>=300:
    print(r.text[:400]); sys.exit(1)
rows=r.json()
tot_rows=tot_bytes=0
print(f"\n{'table':34}{'rows':>10}{'size':>12}")
print("-"*56)
for x in rows:
    tot_rows+=x['rows'] or 0; tot_bytes+=x['bytes'] or 0
    print(f"{x['t']:34}{x['rows']:>10,}{x['size']:>12}")
print("-"*56)
print(f"{'TOTAL ('+str(len(rows))+' tables)':34}{tot_rows:>10,}{tot_bytes/1048576:>10.1f}M")
import json; json.dump([x['t'] for x in rows], open("cc_work/_main_tables.json","w"))
print("\nwrote cc_work/_main_tables.json")
