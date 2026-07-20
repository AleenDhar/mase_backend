"""Precise practice-set watcher: uses updated_at (timestamptz) so same-day re-sweeps are
detected. Read-only. Prints which practice rows have refreshed in the last N minutes."""
import sys, datetime
import dryrun_fleet as D
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DEALS = [("ACEN", "006P700000DkWgX"), ("Allstate", "006P7000006uKrq"),
         ("Cebu Pacific Air", "0066700000wdNe1"), ("Consumer Cellular", "006P700000OcxpH"),
         ("Publicis Groupe", "006P700000Xl06R"), ("John Deere", "006P700000KHd9V")]
now = datetime.datetime.now(datetime.timezone.utc)
for label, oid in DEALS:
    r = D.requests.get(f"{D.SB}/rest/v1/deal_records",
                       params={"select": "updated_at,record", "opp_id": f"eq.{oid}"},
                       headers=D.SH, verify=D.VERIFY, timeout=60).json()
    if not r:
        print(f"{label:20} NO ROW")
        continue
    up = r[0].get("updated_at") or ""
    ds = (((r[0].get("record") or {}).get("ai") or {}).get("deal_scores") or {})
    hl = ds.get("headline") or {}
    sv = ((r[0].get("record") or {}).get("ai") or {}).get("scoring_studio") or {}
    win_v = (sv.get("versions") or {}).get("win")
    try:
        age_min = int((now - datetime.datetime.fromisoformat(up.replace("Z", "+00:00"))).total_seconds() // 60)
    except Exception:
        age_min = "?"
    print(f"{label:20} updated {age_min:>4}m ago | win={hl.get('win_position')} mom={hl.get('deal_momentum')} "
          f"src={ds.get('factor_source')} | win-engine v{win_v}")
