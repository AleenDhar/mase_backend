"""READ-ONLY probe of the 6 forecasted deals in deal_records. No AWS, no dryrun_fleet.

Reads Supabase creds straight from the frontend .env.local so it never touches
`aws secretsmanager` (which stalls for minutes behind Zscaler).

Projects only the nested JSON paths we need — never `select=record` (multi-MB blob).
Answers: is `ai.deal_scores` null (the stale-worker wipe), and what's live vs the local CSV?
"""
import csv, json, os, sys, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"
cfg = {}
for line in open(ENV, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()

SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
KEY = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Accept": "application/json"}

SIX = [("SAMI", "006P700000RD9Ir"), ("Allstate", "006P7000006uKrq"),
       ("Robert Bosch", "006P700000PlMpu"), ("NORTHPORT", "006P700000QFJwD"),
       ("Domino's Pizza", "006P700000X6hvK"), ("Greencore", "006P700000WeRX8")]

SELECT = ("opp_id,updated_at,swept_at,"
          "scores:record->ai->deal_scores,studio:record->ai->scoring_studio")

local = {}
try:
    for r in csv.DictReader(open("cc_fleet_results.csv", encoding="utf-8-sig")):
        local[r["opp_id"]] = (r["win"], r["momentum"])
except Exception:
    pass

print(f"{'deal':16} {'updated_at':21} {'live win/mom':>13} {'src':>8} {'winEng':>7} "
      f"{'local win/mom':>13}  state")
print("-" * 104)
wiped = []
for label, oid in SIX:
    r = requests.get(f"{SB}/rest/v1/deal_records",
                     params={"select": SELECT, "opp_id": f"eq.{oid}"},
                     headers=SH, verify=False, timeout=(10, 45))
    rows = r.json() if r.status_code == 200 else []
    if not rows:
        print(f"{label:16} {'-':21} {'NO ROW':>13}")
        continue
    row = rows[0]
    ds = row.get("scores")
    lw = local.get(oid, ("-", "-"))
    lstr = f"{lw[0]}/{lw[1]}"
    if not ds:
        wiped.append(label)
        print(f"{label:16} {str(row.get('updated_at'))[:19]:21} {'NULL':>13} {'-':>8} {'-':>7} "
              f"{lstr:>13}  *** deal_scores WIPED ***")
        continue
    hl = ds.get("headline") or {}
    sv = (row.get("studio") or {}).get("versions") or {}
    live = f"{hl.get('win_position')}/{hl.get('deal_momentum')}"
    src = ds.get("factor_source")
    flag = ""
    if src != "ai":
        flag = f"  <-- DEGRADED (src={src})"
    print(f"{label:16} {str(row.get('updated_at'))[:19]:21} {live:>13} {str(src):>8} "
          f"{'v' + str(sv.get('win')):>7} {lstr:>13}{flag}")

print()
if wiped:
    print(f"!! {len(wiped)} deal(s) with NULL deal_scores in the live front end: {', '.join(wiped)}")
else:
    print("all six have non-null deal_scores")
