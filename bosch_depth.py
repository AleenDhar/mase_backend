"""What legwork is actually ON the Bosch record — and which fields the engine can see."""
import json, sys, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"
cfg = {}
for _l in open(ENV, encoding="utf-8"):
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
K = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": K, "Authorization": f"Bearer {K}"}
OID = "006P700000PlMpu"

r = requests.get(f"{SB}/rest/v1/deal_records", params={"select": "record", "opp_id": f"eq.{OID}"},
                 headers=SH, verify=False, timeout=(10, 90)).json()[0]["record"]
ai = r.get("ai") or {}
cov = r.get("evidence_coverage") or {}

print("TOP-LEVEL record keys :", sorted(r.keys())[:18])
print("record.ai keys       :", sorted(ai.keys()))
print("evidence_coverage    :", sorted(cov.keys()))
print()

# Meeting ledger, whatever shape it takes
for key in ("meeting_stats", "meetings", "calls", "timeline", "engagement"):
    v = ai.get(key) or r.get(key)
    if v:
        print(f"--- {key} ---")
        print(json.dumps(v, default=str)[:900])
        print()

md = ai.get("meddpicc") or ai.get("meddicc") or {}
if md:
    print("--- MEDDPICC ---")
    for k, v in md.items():
        s = v.get("status") if isinstance(v, dict) else v
        print(f"   {k:22} {s}")
    print()

print("--- DEPTH SIGNALS available on this record ---")
print(f"   calls_discovered      : {cov.get('calls_discovered')}")
print(f"   calls_read            : {cov.get('calls_read')}")
print(f"   distinct buyer attendees: {len(cov.get('avoma_attendees') or [])}")
print(f"   calls_omitted         : {len(cov.get('calls_omitted') or [])}")
print(f"   discovery_method      : {cov.get('discovery_method')}")
print(f"   salesforce_window     : {cov.get('salesforce_window')}")
