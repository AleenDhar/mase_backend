import sys, time, warnings, datetime
from concurrent.futures import ThreadPoolExecutor
warnings.filterwarnings("ignore")
import requests, urllib3; urllib3.disable_warnings()
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
cfg = {}
for l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local", encoding="utf-8"):
    l = l.strip()
    if l and not l.startswith("#") and "=" in l:
        k, v = l.split("=", 1); cfg[k.strip()] = v.strip().strip('"').strip("'")
API = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
AH = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}", "Content-Type": "application/json"}
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/"); K = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": K, "Authorization": f"Bearer {K}"}
SEL = ("updated_at,eng:record->ai->scoring_studio->versions->win,"
       "w:record->ai->deal_scores->headline->win_position,m:record->ai->deal_scores->headline->deal_momentum,"
       "rd:record->ai->deal_scores->headline->read,acct:record->hard->account_name,opp:record->hard->opp_name")
def ts(): return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)).strftime("%H:%M IST")
def rec(oid): return (requests.get(f"{SB}/rest/v1/deal_records", params={"select": SEL, "opp_id": f"eq.{oid}"}, headers=SH, verify=False, timeout=(10, 60)).json() or [{}])[0]
# find all Sabic deals
r = requests.get(f"{SB}/rest/v1/deal_records", params={"select": "opp_id,account_name,opp_name,stage", "account_name": "ilike.*sabic*"}, headers=SH, verify=False, timeout=(10, 60)).json()
sabic = [(x["opp_id"], x.get("account_name"), x.get("opp_name"), x.get("stage")) for x in (r or [])]
print(f"[{ts()}] Sabic deals found: {len(sabic)}", flush=True)
for oid, a, o, st in sabic:
    print(f"    {oid}  {a} / {o} [{st}]", flush=True)
def go(oid, a, o, st):
    b = rec(oid); bu = b.get("updated_at")
    print(f"[{ts()}] BEFORE {o}: win={b.get('w')} mom={b.get('m')} read={b.get('rd')} v{b.get('eng')}", flush=True)
    try: requests.post(f"{API}/api/deal-engine/sweep/{oid}/update-living-memory", headers=AH, json={}, verify=False, timeout=(10, 2500))
    except Exception as e: print(f"[{ts()}] {o} post err {e}", flush=True)
    t0 = time.time()
    while time.time() - t0 < 2900:
        time.sleep(25)
        try: a2 = rec(oid)
        except Exception: continue
        if a2.get("updated_at") != bu and a2.get("w") is not None:
            print(f"[{ts()}] AFTER  {o}: win={a2.get('w')} mom={a2.get('m')} read={a2.get('rd')} v{a2.get('eng')}  DONE", flush=True)
            return
    print(f"[{ts()}] {o} TIMEOUT", flush=True)
if sabic:
    with ThreadPoolExecutor(max_workers=max(1, len(sabic))) as ex:
        list(ex.map(lambda d: go(*d), sabic))
print("SABIC-RUN-DONE", flush=True)
