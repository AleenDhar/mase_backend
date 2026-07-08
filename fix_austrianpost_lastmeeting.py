"""Surgical correction of Austrian Post's 'Last meeting' critical-signal text (user-supplied
corrected facts). Targets ONLY ai.critical_signals[2]; verifies lens + current text before
writing; no re-sweep, pin untouched. --apply to write."""
import sys, json, re
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APPLY = "--apply" in sys.argv
NEW_TEXT = ("Onsite Teil 1 & Teil 2 with CPO Flandorfer — Dirk Fischbach and Amit Shah "
            "(Zycus) met Engelbert Pölki and, in part 2, CPO Flandorfer; discussion centered "
            "on Austrian Post's uncertain rollout timeline (driven by a SAP 4HANA migration in "
            "Turkey consuming SAP resources), a user-based license model with spend-based "
            "analytics pricing, and Zycus's higher-compute 'agentic AI' positioning versus point "
            "agents — concluding with implementation estimates of 3-5 months for phase one "
            "and ~12 months for full adoption.")
NEW_TONE = "neu"   # meeting was substantive/progress, not a warning — was 'warn' on the wrong text

sec = load_secret(); base = sec["SUPABASE_URL"].rstrip("/")
key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
h = {"apikey": key, "Authorization": f"Bearer {key}"}
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
mgmt = f"https://api.supabase.com/v1/projects/{ref}/database/query"; tok = sec["SUPABASE_ACCESS_TOKEN"]

r = requests.get(f"{base}/rest/v1/deal_records",
                 params={"account_name": "ilike.*Austrian*Post*", "active": "eq.true", "select": "opp_id,record"},
                 headers=h, verify=VERIFY, timeout=40).json()[0]
oid = r["opp_id"]
cs = (r["record"].get("ai") or {}).get("critical_signals") or []
idx = next((i for i, s in enumerate(cs) if isinstance(s, dict) and str(s.get("lens", "")).lower() == "last meeting"), None)
assert idx is not None, "no 'Last meeting' lens found"
cur = cs[idx]
print(f"target: ai.critical_signals[{idx}]  opp_id={oid}")
print(f"  OLD lens={cur.get('lens')!r} tone={cur.get('tone')!r}")
print(f"  OLD text: {cur.get('text')[:120]}")
assert "1 Jul onsite confirmed the shortlist" in (cur.get("text") or ""), "current text is not the wrong one — abort"

new_elem = {"lens": cur.get("lens", "Last meeting"), "text": NEW_TEXT, "tone": NEW_TONE}
print(f"\n  NEW tone={NEW_TONE!r}")
print(f"  NEW text: {NEW_TEXT}")
if not APPLY:
    print("\n[DRY RUN] --apply to write."); sys.exit()

blob = json.dumps(new_elem, ensure_ascii=False)
sql = ("update deal_records set record = jsonb_set(record, '{ai,critical_signals," + str(idx) + "}', "
       "$J$" + blob + "$J$::jsonb, false), updated_at = now() "
       "where opp_id = '" + oid + "' returning opp_id")
resp = requests.post(mgmt, headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                     json={"query": sql}, verify=VERIFY, timeout=60)
if resp.status_code >= 300:
    print("WRITE FAILED", resp.status_code, resp.text[:300]); sys.exit(1)
print("\nAPPLIED:", resp.json())

# read-back
back = requests.get(f"{base}/rest/v1/deal_records", params={"opp_id": "eq." + oid, "select": "record"},
                    headers=h, verify=VERIFY, timeout=40).json()[0]
v = (back["record"].get("ai") or {}).get("critical_signals")[idx]
print("VERIFY tone=", v.get("tone"))
print("VERIFY text=", v.get("text")[:110])
print("match:", v.get("text") == NEW_TEXT)
