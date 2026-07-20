import warnings, json; warnings.filterwarnings("ignore")
import requests, urllib3; urllib3.disable_warnings()
cfg = {}
for l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local", encoding="utf-8"):
    l = l.strip()
    if l and not l.startswith("#") and "=" in l:
        k, v = l.split("=", 1); cfg[k.strip()] = v.strip().strip('"').strip("'")
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/"); K = cfg["SUPABASE_SERVICE_ROLE_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}
# SABIC deals
r = requests.get(f"{SB}/rest/v1/deal_records",
                 params={"select": "opp_id,account_name,opp_name,stage,updated_at", "account_name": "ilike.*sabic*"},
                 headers=H, verify=False, timeout=(10, 40)).json()
print("=== SABIC deals ===")
sabic_ids = []
for x in (r or []):
    print(f"  {x['opp_id']}  {x.get('account_name')} / {x.get('opp_name')} [{x.get('stage')}]  updated={str(x.get('updated_at'))[:19]}")
    sabic_ids.append(x["opp_id"])
# their sweep_queue rows
print("=== sweep_queue rows (waiting/working/recent) ===")
q = requests.get(f"{SB}/rest/v1/sweep_queue",
                 params={"select": "opp_id,status,run_id,updated_at,claimed_at,attempts", "order": "updated_at.desc", "limit": "40"},
                 headers=H, verify=False, timeout=(10, 40)).json()
if isinstance(q, list):
    for row in q:
        tag = " <== SABIC" if row.get("opp_id") in sabic_ids else ""
        if row.get("status") in ("waiting", "working") or row.get("opp_id") in sabic_ids:
            print(f"  {row.get('opp_id')}  status={row.get('status'):8} run_id={row.get('run_id')} updated={str(row.get('updated_at'))[:19]}{tag}")
    waiting = sum(1 for x in q if x.get("status") == "waiting")
    working = sum(1 for x in q if x.get("status") == "working")
    print(f"queue summary (recent 40): waiting={waiting} working={working}")
else:
    print("queue query:", q)
json.dump(sabic_ids, open("cc_work/_sabic_ids.json", "w"), indent=2)
