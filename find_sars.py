import sys
import dryrun_fleet as D
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

r = D.requests.get(f"{D.SB}/rest/v1/deal_records",
                   params={"select": "opp_id,account_name,opp_name,stage,updated_at",
                           "account_name": "ilike.*SARS*"},
                   headers=D.SH, verify=D.VERIFY, timeout=60)
print("by account ilike SARS:", r.status_code)
for x in (r.json() or []):
    print(" ", x)

r2 = D.requests.get(f"{D.SB}/rest/v1/deal_records",
                    params={"select": "opp_id,account_name,opp_name,stage,updated_at",
                            "account_name": "ilike.*SOUTH AFRICAN*"},
                    headers=D.SH, verify=D.VERIFY, timeout=60)
print("by account ilike SOUTH AFRICAN:", r2.status_code)
for x in (r2.json() or []):
    print(" ", x)

r3 = D.requests.get(f"{D.SB}/rest/v1/deal_records",
                    params={"select": "opp_id,account_name,opp_name,stage",
                            "opp_name": "ilike.*SARS*"},
                    headers=D.SH, verify=D.VERIFY, timeout=60)
print("by opp_name ilike SARS:", r3.status_code)
for x in (r3.json() or []):
    print(" ", x)
