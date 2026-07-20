"""Full restored Bosch record: scores, provenance, evidence, and the reasons behind them."""
import sys, warnings
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
K = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": K, "Authorization": f"Bearer {K}"}
OID = "006P700000PlMpu"

SEL = ("account_name,stage,close_date,amount,updated_at,"
       "scores:record->ai->deal_scores,studio:record->ai->scoring_studio,"
       "cov:record->evidence_coverage,ceo:record->ai->ceo_intervention,"
       "meet:record->ai->meeting_stats")
r = requests.get(f"{SB}/rest/v1/deal_records", params={"select": SEL, "opp_id": f"eq.{OID}"},
                 headers=SH, verify=False, timeout=(10, 60)).json()[0]
ds = r.get("scores") or {}
hl = ds.get("headline") or {}
sv = (r.get("studio") or {}).get("versions") or {}
cov = r.get("cov") or {}

print("=" * 96)
print(f"ROBERT BOSCH GmbH — {r.get('stage')} | ${r.get('amount')} | close {r.get('close_date')}")
print("=" * 96)
print(f"  WIN {hl.get('win_position')}   MOM {hl.get('deal_momentum')}   "
      f"COMMIT {hl.get('customer_commitment')}   RISK {hl.get('deal_risk')}")
print(f"  read={hl.get('read')!r}  src={ds.get('factor_source')}  "
      f"winEng=v{sv.get('win')} momEng=v{sv.get('mom')}  degraded={ds.get('scoring_degraded')}")
print(f"  evidence: calls_read={cov.get('calls_read')}  coverage={ {k: v for k, v in cov.items() if k != 'calls_read'} }")
print(f"  written : {r.get('updated_at')}")

print("\n  PRIOR VALUES FOR COMPARISON")
print("    cloud api-tier run 15:05 (destroyed by the worker) : win 54 / mom 49")
print("    local harness (six_score_csv, win v10.7)           : win 58 / mom 50")
print("    stale-worker run                                   : null / null")

CATS = [("win_position", "WIN"), ("deal_momentum", "MOMENTUM"),
        ("customer_commitment", "COMMITMENT"), ("deal_risk", "RISK")]
for key, lbl in CATS:
    buls = (ds.get("ai_reasons") or {}).get(key) or []
    if not buls:
        continue
    print(f"\n  -- {lbl} --")
    for b in buls:
        print(f"    [{b.get('tone')}] {b.get('text')}")

ceo = r.get("ceo") or {}
if ceo:
    print(f"\n  CEO: needed={ceo.get('needed')} severity={ceo.get('severity')}")
    if ceo.get("summary"):
        print(f"    {ceo.get('summary')}")
