import json, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
oid = sys.argv[1] if len(sys.argv) > 1 else "006P700000DkWgX"
r = json.load(open(f"cc_work/{oid}.final.json", encoding="utf-8"))
ds = (r.get("ai") or {}).get("deal_scores") or {}
hl = ds.get("headline") or {}
rz = ds.get("ai_reasons") or {}
print(f"WIN {hl.get('win_position')} | MOM {hl.get('deal_momentum')} | "
      f"commit {hl.get('customer_commitment')} | risk {hl.get('deal_risk')} | read {hl.get('read')}")
LBL = {"win_position": "WHY THIS WIN SCORE", "deal_momentum": "WHY THIS MOMENTUM",
       "customer_commitment": "CUSTOMER COMMITMENT", "deal_risk": "DEAL RISK"}
for key in ("win_position", "deal_momentum", "customer_commitment", "deal_risk"):
    print(f"\n== {LBL[key]} ==")
    for b in (rz.get(key) or []):
        print(f"  [{b.get('tone')}] {b.get('text')}")
