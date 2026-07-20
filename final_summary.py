"""Final combined report for the 7 live AWS sweeps — reads deal_records directly.

Prints scores, read label, provenance (must be src=ai + win engine v10.7), CEO intervention,
and the top reasons per score. Writes aws_sweep_results.csv with the full reason sets.
Read-only. No AWS CLI (hangs behind Zscaler); no dryrun_fleet import.
"""
import csv, sys, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"
cfg = {}
for _l in open(ENV, encoding="utf-8"):
    _l = _l.strip()
    if _l and not _l.startswith("#") and "=" in _l:
        k, v = _l.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
SH = {"apikey": cfg["SUPABASE_SERVICE_ROLE_KEY"],
      "Authorization": f"Bearer {cfg['SUPABASE_SERVICE_ROLE_KEY']}"}

DEALS = [("SAMI", "006P700000RD9Ir"), ("Allstate", "006P7000006uKrq"),
         ("Robert Bosch", "006P700000PlMpu"), ("NORTHPORT", "006P700000QFJwD"),
         ("Domino's Pizza", "006P700000X6hvK"), ("Greencore", "006P700000WeRX8"),
         ("SARS", "006P700000UZv8c")]
SEL = ("opp_id,account_name,stage,forecast_category,amount,close_date,updated_at,"
       "scores:record->ai->deal_scores,studio:record->ai->scoring_studio,"
       "ceo:record->ai->ceo_intervention,cov:record->evidence_coverage")
CATS = [("win_position", "WIN"), ("deal_momentum", "MOMENTUM"),
        ("customer_commitment", "COMMITMENT"), ("deal_risk", "RISK")]
NBUL = int(sys.argv[1]) if len(sys.argv) > 1 else 3
PRIOR = {"SAMI": "46/60", "Allstate": "24/8", "Robert Bosch": "46/58", "NORTHPORT": "27/8",
         "Domino's Pizza": "58/58", "Greencore": "18/22", "SARS": "52/65 (hybrid)"}

rows = []
for lbl, oid in DEALS:
    r = requests.get(f"{SB}/rest/v1/deal_records", params={"select": SEL, "opp_id": f"eq.{oid}"},
                     headers=SH, verify=False, timeout=(10, 60)).json()
    rows.append((lbl, oid, r[0] if r else None))

print("=" * 104)
print(f"{'deal':16}{'WIN':>5}{'MOM':>5}{'COM':>5}{'RSK':>5}  {'read':<17}{'src':>7} {'winEng':>7}"
      f"  {'was':>16}")
print("=" * 104)
for lbl, oid, r in rows:
    if not r:
        print(f"{lbl:16}  NO ROW"); continue
    ds = r.get("scores") or {}
    hl = ds.get("headline") or {}
    sv = (r.get("studio") or {}).get("versions") or {}
    deg = "  DEGRADED!" if ds.get("scoring_degraded") else ""
    print(f"{lbl:16}{str(hl.get('win_position')):>5}{str(hl.get('deal_momentum')):>5}"
          f"{str(hl.get('customer_commitment')):>5}{str(hl.get('deal_risk')):>5}  "
          f"{str(hl.get('read')):<17}{str(ds.get('factor_source')):>7} {'v' + str(sv.get('win')):>7}"
          f"  {PRIOR.get(lbl, '-'):>16}{deg}")

for lbl, oid, r in rows:
    if not r:
        continue
    ds = r.get("scores") or {}
    hl = ds.get("headline") or {}
    cov = r.get("cov") or {}
    print("\n" + "=" * 104)
    print(f"### {lbl} — {r.get('account_name')}  ({r.get('stage')} | ${r.get('amount')} | "
          f"close {r.get('close_date')})")
    print(f"    WIN {hl.get('win_position')} | MOM {hl.get('deal_momentum')} | "
          f"COMMIT {hl.get('customer_commitment')} | RISK {hl.get('deal_risk')}   "
          f"read: {hl.get('read')}   calls_read={cov.get('calls_read')}")
    for key, cl in CATS:
        buls = (ds.get("ai_reasons") or {}).get(key) or []
        if not buls:
            continue
        print(f"\n  -- {cl} --")
        for b in buls[:NBUL]:
            print(f"    [{b.get('tone')}] {b.get('text')}")
    ceo = r.get("ceo") or {}
    if ceo:
        print(f"\n  CEO: needed={ceo.get('needed')} severity={ceo.get('severity')}")
        if ceo.get("summary"):
            print(f"    {ceo.get('summary')}")

with open("aws_sweep_results.csv", "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(["deal", "opp_id", "account", "stage", "forecast", "amount", "close_date",
                "win", "momentum", "commitment", "risk", "read", "factor_source", "win_engine",
                "scoring_degraded", "calls_read", "ceo_needed", "ceo_summary",
                "win_reasons", "momentum_reasons", "commitment_reasons", "risk_reasons"])
    for lbl, oid, r in rows:
        if not r:
            w.writerow([lbl, oid] + [""] * 20); continue
        ds = r.get("scores") or {}
        hl = ds.get("headline") or {}
        sv = (r.get("studio") or {}).get("versions") or {}
        ceo = r.get("ceo") or {}
        rs = ds.get("ai_reasons") or {}

        def j(k):
            return " || ".join(f"[{b.get('tone')}] {b.get('text')}" for b in (rs.get(k) or []))
        w.writerow([lbl, oid, r.get("account_name"), r.get("stage"), r.get("forecast_category"),
                    r.get("amount"), r.get("close_date"), hl.get("win_position"),
                    hl.get("deal_momentum"), hl.get("customer_commitment"), hl.get("deal_risk"),
                    hl.get("read"), ds.get("factor_source"), sv.get("win"),
                    ds.get("scoring_degraded"), (r.get("cov") or {}).get("calls_read"),
                    ceo.get("needed"), ceo.get("summary"),
                    j("win_position"), j("deal_momentum"),
                    j("customer_commitment"), j("deal_risk")])
print("\nwrote aws_sweep_results.csv")
