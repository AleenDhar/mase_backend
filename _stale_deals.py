"""Find deals whose Salesforce data is NEWER than MASE last swept (the SFDC trigger was
off, so these never refreshed). Compares current SFDC LastModifiedDate / LastActivityDate
against MASE's swept_at. Read-only. Writes cc_work/_stale_deals.json + prints a summary."""
import boot_env; boot_env.hydrate(verbose=True)
import os, sys, json, warnings
from datetime import datetime, timezone
warnings.filterwarnings("ignore")
import requests, urllib3; urllib3.disable_warnings()
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
from simple_salesforce import Salesforce

SB = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
SH = {"apikey": K, "Authorization": f"Bearer {K}"}

def parse(dt):
    if not dt: return None
    try:
        s = str(dt).replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None

# 1) MASE book: opp_id, swept_at, account, stage
rows, off = [], 0
while True:
    r = requests.get(f"{SB}/rest/v1/deal_records",
                     params={"select": "opp_id,account_name,stage,swept:record->>swept_at,upd:updated_at",
                             "limit": "1000", "offset": str(off)}, headers=SH, verify=False, timeout=(10, 90)).json()
    if not isinstance(r, list) or not r: break
    rows += r
    if len(r) < 1000: break
    off += 1000
mase = {x["opp_id"]: x for x in rows if x.get("opp_id")}
print(f"MASE tracked deals: {len(mase)}", flush=True)

# 2) Salesforce current LastModifiedDate / LastActivityDate for those opps (chunked)
_sfsess = requests.Session(); _sfsess.verify = False  # Zscaler MITM cert — same as boto3 verify=False
sf = Salesforce(username=os.environ["SF_USERNAME"], password=os.environ["SF_PASSWORD"],
                security_token=os.environ["SF_SECURITY_TOKEN"], domain=os.environ.get("SF_DOMAIN", "login"),
                session=_sfsess)
ids = list(mase.keys())
sfdata = {}
for i in range(0, len(ids), 150):
    chunk = ids[i:i + 150]
    inlist = ",".join("'" + c.replace("'", "") + "'" for c in chunk)
    q = f"SELECT Id, Name, StageName, LastModifiedDate, LastActivityDate FROM Opportunity WHERE Id IN ({inlist})"
    try:
        for rec in sf.query_all(q)["records"]:
            sfdata[rec["Id"]] = rec
            sfdata[rec["Id"][:15]] = rec
    except Exception as e:
        print(f"SOQL chunk {i} err: {e}", flush=True)
print(f"SFDC opps returned: {len(set(r['Id'] for r in sfdata.values()))}", flush=True)

# 3) compare
stale = []
for oid, m in mase.items():
    sfrec = sfdata.get(oid) or sfdata.get(oid[:15])
    if not sfrec: continue
    swept = parse(m.get("swept") or m.get("upd"))
    lmd = parse(sfrec.get("LastModifiedDate"))
    lad = parse(sfrec.get("LastActivityDate"))
    if not swept: continue
    reasons = []
    if lmd and lmd > swept: reasons.append("field-change")
    if lad and lad > swept: reasons.append("new-activity")
    if reasons:
        days = round((max(x for x in (lmd, lad) if x) - swept).total_seconds() / 86400, 1)
        stale.append({"opp_id": oid, "account": m.get("account_name"), "stage": m.get("stage"),
                      "swept_at": str(swept)[:16], "sf_last_modified": str(lmd)[:16] if lmd else None,
                      "sf_last_activity": str(lad)[:10] if lad else None, "days_newer": days,
                      "reason": "+".join(reasons)})

stale.sort(key=lambda x: -x["days_newer"])
json.dump(stale, open("cc_work/_stale_deals.json", "w", encoding="utf-8"), indent=2, default=str)
print(f"\n=== STALE deals (SFDC newer than MASE): {len(stale)} of {len(mase)} ===", flush=True)
na = sum(1 for s in stale if "new-activity" in s["reason"])
print(f"    of which NEW ACTIVITY since last sweep: {na}", flush=True)
for s in stale[:30]:
    print(f"  {str(s['account'])[:32]:32} [{str(s['stage'])[:16]:16}] swept {s['swept_at']}  SF-mod {s['sf_last_modified']}  +{s['days_newer']}d  ({s['reason']})", flush=True)
print(f"\nwrote cc_work/_stale_deals.json ({len(stale)} deals)", flush=True)
