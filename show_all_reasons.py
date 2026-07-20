import csv, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
rows = list(csv.DictReader(open("cc_fleet_results.csv", encoding="utf-8-sig")))
ORDER = ["Robert Bosch", "Domino", "SAMI", "NORTHPORT", "Allstate", "Greencore"]
rows.sort(key=lambda r: next((i for i, o in enumerate(ORDER) if o in r["account"]), 99))
CATS = [("win_reasons", "WIN POSITION"), ("momentum_reasons", "DEAL MOMENTUM"),
        ("commitment_reasons", "CUSTOMER COMMITMENT"), ("risk_reasons", "DEAL RISK")]
for r in rows:
    print("\n" + "=" * 92)
    print(f"### {r['account']}  ({r['stage']} | ${r['amount']} | close {r['close_date']})")
    print(f"    WIN {r['win']} | MOM {r['momentum']} | COMMIT {r['commitment']} | RISK {r['risk']} "
          f"| read: {r['read']}")
    for key, lbl in CATS:
        hdr = {"win_reasons": r["win"], "momentum_reasons": r["momentum"],
               "commitment_reasons": r["commitment"], "risk_reasons": r["risk"]}[key]
        print(f"\n  -- {lbl} ({hdr}) --")
        for b in (r[key] or "").split(" || "):
            if b.strip():
                print(f"    - {b}")
