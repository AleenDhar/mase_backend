import warnings, json; warnings.filterwarnings("ignore")
import requests, urllib3; urllib3.disable_warnings()
cfg = {}
for l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local", encoding="utf-8"):
    l = l.strip()
    if l and not l.startswith("#") and "=" in l:
        k, v = l.split("=", 1); cfg[k.strip()] = v.strip().strip('"').strip("'")
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/"); K = cfg["SUPABASE_SERVICE_ROLE_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}
# All completed/pushed to-do rows -> the deals that have a ticked-off to-do.
rows = []
off = 0
while True:
    r = requests.get(f"{SB}/rest/v1/deal_todo_pushes",
                     params={"select": "opp_id,todo_key,category,subject,pushed_at,sf_task_id", "limit": "1000", "offset": str(off)},
                     headers=H, verify=False, timeout=(10, 60))
    if r.status_code >= 300:
        print("HTTP", r.status_code, r.text[:300]); break
    j = r.json()
    if not isinstance(j, list) or not j:
        break
    rows += j
    if len(j) < 1000:
        break
    off += 1000
opps = {}
for x in rows:
    oid = x.get("opp_id")
    if oid:
        opps.setdefault(oid, 0)
        opps[oid] += 1
print(f"completed/pushed to-do rows: {len(rows)}")
print(f"distinct deals with >=1 ticked-off to-do: {len(opps)}")
print("sample (opp_id : #completed):", dict(list(sorted(opps.items(), key=lambda kv: -kv[1]))[:8]))
json.dump(sorted(opps.keys()), open("cc_work/_completed_todo_opps.json", "w"), indent=2)
print("wrote cc_work/_completed_todo_opps.json")
