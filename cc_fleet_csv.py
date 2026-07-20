"""Collect cc_work/*.row.json -> cc_fleet_results.csv (the user's review artifact)."""
import csv, glob, json, os, sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FIELDS = ["opp_id", "account", "opp_name", "stage", "forecast_category", "amount", "close_date",
          "pinned", "win", "momentum", "commitment", "risk", "forecast_confidence", "read",
          "factor_source", "win_reasons", "momentum_reasons", "commitment_reasons", "risk_reasons",
          "forecast_defensible", "forecast_recommended", "day_summary",
          "ceo_needed", "ceo_severity", "ceo_summary", "ceo_reason_types",
          "stakeholders_n", "moves_n", "competitors", "calls_discovered", "calls_read", "confidence"]

rows = []
for p in sorted(glob.glob(os.path.join("cc_work", "*.row.json"))):
    try:
        rows.append(json.load(open(p, encoding="utf-8")))
    except Exception as e:  # noqa: BLE001
        print(f"skip {p}: {e}")

with open("cc_fleet_results.csv", "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
print(f"cc_fleet_results.csv written — {len(rows)} deals")
scored = [r for r in rows if r.get("win") is not None]
print(f"scored: {len(scored)} | ceo_needed: {sum(1 for r in rows if r.get('ceo_needed'))} "
      f"| pinned: {sum(1 for r in rows if r.get('pinned'))}")
