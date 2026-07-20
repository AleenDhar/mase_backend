import json, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
for oid, label in [("006P7000006uKrq", "Allstate"), ("006P700000OcxpH", "Consumer Cellular")]:
    rec = json.load(open(f"cc_work/{oid}.final.json", encoding="utf-8"))
    ai = rec.get("ai") or {}
    ds = ai.get("deal_scores") or {}
    hl = ds.get("headline") or {}
    print(f"\n### {label} — WIN {hl.get('win_position')} / MOM {hl.get('deal_momentum')} ###")
    print("-- win_position reasons --")
    for b in (ds.get("ai_reasons", {}).get("win_position") or [])[:6]:
        print("  •", b.get("text"))
    ceo = ai.get("ceo_intervention") or {}
    print("-- CEO --  needed:", ceo.get("needed"), "| summary:", repr(ceo.get("summary")))
