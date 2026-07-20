import json, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SIX = [("SAMI", "006P700000RD9Ir"), ("Allstate", "006P7000006uKrq"),
       ("Robert Bosch GmbH", "006P700000PlMpu"), ("NORTHPORT (MALAYSIA)", "006P700000QFJwD"),
       ("Domino's Pizza", "006P700000X6hvK"), ("Greencore Group", "006P700000WeRX8")]
for label, oid in SIX:
    try:
        r = json.load(open(f"cc_work/{oid}.final.json", encoding="utf-8"))
    except Exception as e:
        print(f"\n### {label}: no final.json ({e})")
        continue
    ai = r.get("ai") or {}
    ds = ai.get("deal_scores") or {}
    hl = ds.get("headline") or {}
    rz = ds.get("ai_reasons") or {}
    hard = r.get("hard") or {}
    ceo = ai.get("ceo_intervention") or {}
    fr = ai.get("forecast_read") or {}
    print(f"\n{'='*95}\n### {label} — {hard.get('stage')} · ${hard.get('amount')} · close {hard.get('close_date')}")
    print(f"WIN {hl.get('win_position')} | MOM {hl.get('deal_momentum')} | commit {hl.get('customer_commitment')} "
          f"| risk {hl.get('deal_risk')} | fc {hl.get('forecast_confidence')} | read {hl.get('read')} "
          f"| src {ds.get('factor_source')}")
    for key, l in (("win_position", "WHY WIN"), ("deal_momentum", "WHY MOMENTUM")):
        print(f"  -- {l} --")
        for b in (rz.get(key) or []):
            print(f"    • {b.get('text')}")
    print(f"  FORECAST: defensible={fr.get('defensible')} -> {fr.get('recommended_forecast')}")
    print(f"  CEO: needed={ceo.get('needed')} sev={ceo.get('severity')} — {ceo.get('summary')}")
