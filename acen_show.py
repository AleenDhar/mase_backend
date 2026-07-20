import json, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
r = json.load(open("cc_work/006P700000DkWgX.final.json", encoding="utf-8"))
ai = r.get("ai") or {}
hard = r.get("hard") or {}
ds = ai.get("deal_scores") or {}
hl = ds.get("headline") or {}
print(f"ACEN — {hard.get('stage')} · ${hard.get('amount')} · close {hard.get('close_date')} · forecast {hard.get('forecast_category')}")
print(f"\nWIN {hl.get('win_position')} | MOM {hl.get('deal_momentum')} | read={hl.get('read')} | "
      f"commit={hl.get('customer_commitment')} risk={hl.get('deal_risk')} fc={hl.get('forecast_confidence')} | src={ds.get('factor_source')}")
rz = ds.get("ai_reasons") or {}
for key in ("win_position", "deal_momentum"):
    print(f"\n-- {key} --")
    for b in (rz.get(key) or []):
        print("  •", b.get("text"))
fr = ai.get("forecast_read") or {}
print(f"\n-- forecast_read -- defensible={fr.get('defensible')} recommend={fr.get('recommended_forecast')}")
print("  ", (fr.get("reason") or "")[:400])
md = ai.get("meddpicc") or {}
print("\n-- MEDDPICC --")
for k in ("metrics", "economic_buyer", "decision_criteria", "decision_process", "paper_process", "identify_pain", "champion", "competition"):
    v = md.get(k) or {}
    print(f"  {k}: {v.get('status')}")
stk = (ai.get("stakeholder_map") or {}).get("items") or []
print("\n-- stakeholders --")
for s in stk:
    print(f"  {s.get('name')} ({s.get('title')}) — {s.get('role')} | {s.get('sentiment')}")
cp = ai.get("competitive_position") or {}
print("\n-- competitive --")
for c in (cp.get("competitors") or []):
    print(f"  {c.get('name')} — {c.get('threat_level')}/{c.get('status')}")
rm = (ai.get("recommended_moves") or {}).get("items") or []
print("\n-- moves --")
for m in rm:
    print(f"  [r{m.get('rank')}] ({m.get('horizon')}) {m.get('action')} — act_by {m.get('act_by')}")
ceo = ai.get("ceo_intervention") or {}
print(f"\n-- CEO -- needed={ceo.get('needed')} | summary: {ceo.get('summary')!r}")
