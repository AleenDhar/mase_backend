"""Resolve the 6 validation opps -> opp_ids from Supabase deal_records (match on account name)."""
import sys, json, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
cfg = {}
for _l in open(r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local", encoding="utf-8"):
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1); cfg[k.strip()] = v.strip().strip('"').strip("'")
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/"); K = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": K, "Authorization": f"Bearer {K}"}
TARGETS = ["Bright Horizons", "Austrian Post", "Robert Bosch", "Publicis", "SARS", "Birmingham"]

# pull opp_id + account + opp name + stage for all records, match locally (robust to column shape)
r = requests.get(f"{SB}/rest/v1/deal_records",
                 params={"select": "opp_id,account_name,opp_name,stage,updated_at"},
                 headers=SH, verify=False, timeout=(10, 90))
rows = r.json() if r.status_code < 300 else []
if not isinstance(rows, list):
    print("QUERY ERR", r.status_code, str(rows)[:300]); raise SystemExit(1)
print(f"scanned {len(rows)} records")
out = {}
for t in TARGETS:
    tl = t.lower()
    hits = [x for x in rows if tl in (str(x.get("account_name") or "") + " " + str(x.get("opp_name") or "")).lower()]
    out[t] = hits
    if not hits:
        print(f"  {t:22} -> NONE FOUND")
    for h in hits[:5]:
        print(f"  {t:22} -> {h.get('opp_id')}  acct='{h.get('account_name')}' opp='{h.get('opp_name')}' [{h.get('stage')}]")
json.dump(out, open("cc_work/_validation_opps.json", "w", encoding="utf-8"), indent=2)
print("wrote cc_work/_validation_opps.json")
