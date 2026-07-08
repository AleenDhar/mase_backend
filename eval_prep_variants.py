"""EVAL prep — build the strict/loose momentum instruction variants (base = the
CURRENTLY LOCKED mom v10.2 text + a clearly-marked calibration override section)
and the eval deal set (10 forecasted + Galp, all non-pinned)."""
import csv, json, sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = open("eval_original_mom_locked.txt", encoding="utf-8").read().rstrip() + "\n"

STRICT = BASE + """
## 9. EVAL CALIBRATION OVERRIDE — STRICT READING (temporary QA probe)
This locked version TIGHTENS the reading discipline for a controlled evaluation. Where this
section conflicts with anything above, THIS SECTION WINS.
- VERDICT: classify momentum as Slowing unless there is a buyer-side forward ACTION within the
  last 14 days. "Steady" or better requires a buyer-confirmed next milestone.
- PLAN / MILESTONES: extract and credit ONLY buyer-confirmed future milestones (buyer-voiced,
  buyer-accepted, or buyer-owned deliverables in a live process). Rep-typed / vendor-authored
  dates with no buyer confirmation are NOT a plan — exclude them from every plan/milestone signal.
- ENGAGEMENT READING: a session counts as buyer engagement only with clear evidence of buyer
  attendance/participation. Pricing or renegotiation sessions on a deal whose amount or forecast
  is declining are DEFENSIVE activity — grade them at half depth and say so in the rationale.
- PROCESS-MODE: claim a live RFP/process ONLY with an explicit dated buyer deliverable within the
  next 30 days. A deadline that passed in silence means the process is over.
- RATIONALE: lead with the sharpest ⚠️ gap. Never open with a ✅ on a deal that is declining on
  forecast or amount.
"""

LOOSE = BASE + """
## 9. EVAL CALIBRATION OVERRIDE — GENEROUS READING (temporary QA probe)
This locked version RELAXES the reading discipline slightly for a controlled evaluation. Where
this section conflicts with anything above, THIS SECTION WINS.
- VERDICT: give the deal the benefit of the doubt — if ANY dated future milestone exists,
  classify the verdict no lower than Steady.
- PLAN / MILESTONES: any dated future milestone counts as plan signal, including rep-planned
  ones; a written forward plan is itself evidence of motion.
- ENGAGEMENT READING: a consistent meeting cadence (2+ sessions in the last 45 days) reads as
  building momentum even when the sessions are shallow or defensive.
- CLOSE DATE: treat pushes of up to 90 days as neutral timing, not slippage.
- RATIONALE: lead with the strongest ✅; frame gaps as opportunities rather than warnings.
"""

open("eval_variant_strict.txt", "w", encoding="utf-8").write(STRICT)
open("eval_variant_loose.txt", "w", encoding="utf-8").write(LOOSE)
print(f"variants written: strict {len(STRICT)} chars · loose {len(LOOSE)} chars (base {len(BASE)})")

WANT = ["S&C Electric Company", "DOMINO'S PIZZA", "BMI Group", "Consumer Cellular", "SAMI",
        "Robert Bosch", "Khansaheb", "Foundever", "Roivant"]
CADENCE_OPP = "CadenceDesign_CLM'26"
deals = [{"opp_id": "006P700000AfJyb", "account": "Galp Energia", "opportunity": "Galp_S2P",
          "baseline_win": 19.5, "baseline_mom": 46.0}]  # v2-logic local (substance gate)
rows = list(csv.DictReader(open(r"C:\Users\Aleen.Dhar\Desktop\forecasted_scores_local_v2.csv",
                                encoding="utf-8-sig")))
for r in rows:
    hit = any(r["account"].upper().startswith(w.upper()) for w in WANT) or r["opportunity"] == CADENCE_OPP
    if hit and r["pinned"] == "False":
        deals.append({"opp_id": r["opp_id"], "account": r["account"], "opportunity": r["opportunity"],
                      "baseline_win": float(r["win_local"]), "baseline_mom": float(r["momentum_local"])})
json.dump(deals, open("eval_deals.json", "w"), indent=1)
print(f"eval set: {len(deals)} deals")
for d in deals:
    print(f"  {d['account'][:34]:34s} {d['opportunity'][:36]:36s} base win={d['baseline_win']:5.1f} mom={d['baseline_mom']:5.1f}")
