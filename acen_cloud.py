"""Dry-run sweep ACEN on the DEPLOYED pipeline (td282) -> ab_test_results (NO deal_records write).
Prints the cloud scores so we can compare vs the local run (win 40 / mom 8)."""
import json, sys
import dryrun_fleet as D
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROW = [{"opp_id": "006P700000DkWgX", "opp_name": "ACEN S2P", "account_name": "ACEN"}]
try:
    h = D.requests.get(f"{D.API}/api/health", headers=D.AH, verify=False, timeout=30)
    print(f"API {D.API} health -> {h.status_code}", flush=True)
except Exception as e:
    print("health failed:", e, flush=True)
    sys.exit(1)

D.run_batch(ROW, validate_mode=False)

st = D.poll("006P700000DkWgX") or {}
res = st.get("result") if isinstance(st.get("result"), dict) else {}
rec = (res or {}).get("record")
if not rec:
    print("NO RECORD  status=", st.get("status"), "err=", str(st.get("error"))[:120], flush=True)
    sys.exit(0)
json.dump(rec, open("dryrun_forecasted/006P700000DkWgX.json", "w", encoding="utf-8"), indent=1, default=str)
ai = rec.get("ai") or {}
ds = ai.get("deal_scores") or {}
hl = ds.get("headline") or {}
ev = rec.get("evidence_coverage") or {}
ceo = ai.get("ceo_intervention") or {}
comps = (ai.get("competitive_position") or {}).get("competitors") or []
print("\n===== ACEN CLOUD (td282) =====", flush=True)
print(f"WIN {hl.get('win_position')} | MOM {hl.get('deal_momentum')} | read={hl.get('read')} | "
      f"commit={hl.get('customer_commitment')} risk={hl.get('deal_risk')} fc={hl.get('forecast_confidence')} "
      f"| src={ds.get('factor_source')} degraded={ds.get('scoring_degraded')}", flush=True)
print(f"calls_read={ev.get('calls_read')} of {ev.get('calls_discovered')} | "
      f"CEO needed={ceo.get('needed')} summary={ceo.get('summary')!r}", flush=True)
print("competitors:", [c.get("name") for c in comps], flush=True)
print("\n-- win_position reasons --", flush=True)
for b in (ds.get("ai_reasons", {}).get("win_position") or []):
    print("  •", b.get("text"), flush=True)
