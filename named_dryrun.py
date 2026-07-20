"""DRY-RUN sweep (datalake-test -> datalake.ab_test_results; NOTHING to deal_records) for the
user's named opps, on the DEPLOYED pipeline now running locked sweep 10.1 + win 10.4. Reuses
dryrun_fleet's start/poll/run_batch. Saves each would-be record locally under dryrun_forecasted/.

  python named_dryrun.py canary   # Allstate only (validate 10.1 fixes the fetch)
  python named_dryrun.py all       # Allstate + Telcel + GSK + Saudia
"""
import sys, requests
import dryrun_fleet as D

ALL = [
    {"opp_id": "006P7000006uKrq", "opp_name": "Allstate S2P",             "account_name": "Allstate"},
    {"opp_id": "006P700000aBK6l", "opp_name": "Telcel_S2P_June26",        "account_name": "Telcel"},
    {"opp_id": "006P700000aZ93k", "opp_name": "GSK_S2P_2026",             "account_name": "GSK plc"},
    {"opp_id": "006P700000aEeX8", "opp_name": "Saudia Airlines_S2P_2026", "account_name": "Saudia Airlines"},
]

mode = sys.argv[1] if len(sys.argv) > 1 else "canary"
rows = ALL[:1] if mode == "canary" else ALL

# health check first so we fail fast instead of hanging
try:
    h = requests.get(f"{D.API}/api/health", headers=D.AH, verify=False, timeout=30)
    print(f"API {D.API} health -> {h.status_code}", flush=True)
except Exception as e:
    print(f"API health FAILED: {type(e).__name__}: {e}", flush=True)
    sys.exit(1)

print(f"named dry-run [{mode}]: {[r['account_name'] for r in rows]}", flush=True)
res = D.run_batch(rows, validate_mode=False)
D.write_csv(res)
print("\n=== RESULT ===", flush=True)
for r in res:
    print(f"  {r.get('account'):18} status={r.get('status')} win={r.get('win_dryrun')} "
          f"mom={r.get('mom_dryrun')} src={r.get('factor_source')} calls={r.get('calls_read')} "
          f"verdict={str(r.get('verdict'))[:40]} err={str(r.get('error'))[:80]}", flush=True)
