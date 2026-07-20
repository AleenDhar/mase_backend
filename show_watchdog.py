"""Final combined summary of the 7 live AWS sweeps (reads cc_work/_watchdog_results.json).
Also writes aws_sweep_results.csv for review."""
import csv, json, sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

rows = json.load(open("cc_work/_watchdog_results.json", encoding="utf-8"))
NBUL = int(sys.argv[1]) if len(sys.argv) > 1 else 3
CATS = [("win_position", "WIN"), ("deal_momentum", "MOMENTUM"),
        ("customer_commitment", "COMMITMENT"), ("deal_risk", "RISK")]

print("=" * 100)
print(f"{'deal':17}{'WIN':>5}{'MOM':>5}{'COM':>5}{'RSK':>5}  {'read':<17}{'QA':>6}  {'src':>7} retries")
print("=" * 100)
for o in rows:
    if o.get("status") != "OK":
        print(f"{o['label']:17}  FAILED — needs a human"); continue
    print(f"{o['label']:17}{o['win']:>5}{o['mom']:>5}{o['commit']:>5}{o['risk']:>5}  "
          f"{str(o['read']):<17}{o['acc']:>6}  {str(o['src']):>7} {o['retries']}")

for o in rows:
    if o.get("status") != "OK":
        continue
    print("\n" + "=" * 100)
    print(f"### {o['label']}   WIN {o['win']} | MOM {o['mom']} | COMMIT {o['commit']} | "
          f"RISK {o['risk']}   read: {o['read']}")
    cov = o.get("cov") or {}
    if cov:
        print(f"    evidence: calls_read={cov.get('calls_read')} ")
    for key, lbl in CATS:
        buls = (o.get("reasons") or {}).get(key) or []
        if not buls:
            continue
        print(f"\n  -- {lbl} --")
        for b in buls[:NBUL]:
            tone = b.get("tone", "?")
            print(f"    [{tone}] {b.get('text')}")
    ceo = o.get("ceo") or {}
    if ceo:
        print(f"\n  CEO intervention: needed={ceo.get('needed')} severity={ceo.get('severity')}")
        if ceo.get("summary"):
            print(f"    {ceo.get('summary')}")

with open("aws_sweep_results.csv", "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["deal", "opp_id", "status", "win", "momentum", "commitment", "risk", "read",
                "factor_source", "qa_accuracy", "retries", "ceo_needed", "ceo_summary",
                "win_reasons", "momentum_reasons", "commitment_reasons", "risk_reasons"])
    for o in rows:
        if o.get("status") != "OK":
            w.writerow([o["label"], o["oid"], "FAILED"] + [""] * 14); continue
        r = o.get("reasons") or {}

        def j(k):
            return " || ".join(f"[{b.get('tone')}] {b.get('text')}" for b in (r.get(k) or []))
        ceo = o.get("ceo") or {}
        w.writerow([o["label"], o["oid"], "OK", o["win"], o["mom"], o["commit"], o["risk"],
                    o["read"], o["src"], o["acc"], o["retries"],
                    ceo.get("needed"), ceo.get("summary"),
                    j("win_position"), j("deal_momentum"),
                    j("customer_commitment"), j("deal_risk")])
print("\nwrote aws_sweep_results.csv")
