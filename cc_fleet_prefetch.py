"""Fleet prefetch: pull SF + Avoma data for ALL active forecast_critical deals into cc_work/
(resume-safe, Zscaler-friendly, $0). Writes cc_fleet_ids.json (the work list for the workflow)."""
import json, sys
import dryrun_fleet as D
import cc_sweep
from daily_summary.common import id15
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

rows = D.forecasted()
fleet = [{"opp_id": id15(r["opp_id"]), "account": r.get("account_name"), "opp_name": r.get("opp_name"),
          "stage": r.get("stage"), "amount": r.get("amount")} for r in rows]
json.dump(fleet, open("cc_fleet_ids.json", "w", encoding="utf-8"), indent=1, default=str)
print(f"fleet: {len(fleet)} forecasted deals -> cc_fleet_ids.json", flush=True)
cc_sweep.prefetch_to_files([f["opp_id"] for f in fleet], resume=False, workers=12)
