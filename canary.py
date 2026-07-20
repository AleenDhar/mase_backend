"""1-deal CANARY gating the 6-deal fan-out, per commit 9b3e003:
   "A 1-deal canary (worker run logs claude-sonnet-5 + writes non-null deal_scores)
    gates the full fan-out."

Fires ONE live manual trigger (default: NORTHPORT, whose live deal_scores is NULL — so a
pass also repairs the broken front-end row), then watches deal_trigger_runs + deal_records
until the run completes. PASS iff: deal_scores non-null AND factor_source == 'ai' AND the
run model is claude-sonnet-5. No AWS; creds from frontend/.env.local.

  python canary.py                 # NORTHPORT
  python canary.py 006P700000RD9Ir SAMI
"""
import sys, time, datetime, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
urllib3.disable_warnings()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"
cfg = {}
for line in open(ENV, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
SB = cfg["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/")
KEY = cfg["SUPABASE_SERVICE_ROLE_KEY"]
SH = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Accept": "application/json"}
API = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
AH = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}", "Content-Type": "application/json"}

OID = sys.argv[1] if len(sys.argv) > 1 else "006P700000QFJwD"
LABEL = sys.argv[2] if len(sys.argv) > 2 else "NORTHPORT"
TIMEOUT_S = 1500
POLL_S = 30


def dr():
    r = requests.get(f"{SB}/rest/v1/deal_records",
                     params={"select": "updated_at,swept_at,scores:record->ai->deal_scores,"
                                        "studio:record->ai->scoring_studio", "opp_id": f"eq.{OID}"},
                     headers=SH, verify=False, timeout=(10, 45))
    j = r.json()
    return j[0] if isinstance(j, list) and j else None


def latest_run():
    r = requests.get(f"{SB}/rest/v1/deal_trigger_runs",
                     params={"select": "status,source,model,duration_ms,error,created_at,"
                                       "total_tokens,validation_violations",
                             "opp_id": f"eq.{OID}", "order": "created_at.desc", "limit": "1"},
                     headers=SH, verify=False, timeout=(10, 45))
    j = r.json()
    return j[0] if isinstance(j, list) and j else None


def scoreline(rec):
    if not rec:
        return "NO ROW"
    ds = rec.get("scores")
    if not ds:
        return "deal_scores=NULL"
    hl = ds.get("headline") or {}
    sv = (rec.get("studio") or {}).get("versions") or {}
    return (f"win={hl.get('win_position')} mom={hl.get('deal_momentum')} "
            f"commit={hl.get('customer_commitment')} risk={hl.get('deal_risk')} "
            f"src={ds.get('factor_source')} winEng=v{sv.get('win')} momEng=v{sv.get('mom')}")


base = dr()
base_upd = (base or {}).get("updated_at")
base_run = latest_run()
base_run_ts = (base_run or {}).get("created_at")
print(f"=== CANARY: {LABEL} ({OID}) ===")
print(f"  baseline: updated_at={str(base_upd)[:19]}  {scoreline(base)}")
print(f"  baseline latest run: {str(base_run_ts)[:19]} status={(base_run or {}).get('status')} "
      f"model={(base_run or {}).get('model')}")

r = requests.post(f"{API}/api/deal-engine/sweep/trigger", headers=AH,
                  json={"opp_id": OID, "source": "manual"}, verify=False, timeout=60)
print(f"\n  [trigger] HTTP {r.status_code}  {r.text[:160]}")
if r.status_code >= 300:
    print("  !! trigger rejected — aborting canary")
    sys.exit(2)

t0 = time.time()
print(f"\n  [watch] polling every {POLL_S}s (timeout {TIMEOUT_S // 60}m)…\n")
run_seen = None
while time.time() - t0 < TIMEOUT_S:
    time.sleep(POLL_S)
    age = int(time.time() - t0)
    run = latest_run()
    rec = dr()
    new_run = run and run.get("created_at") != base_run_ts
    status = (run or {}).get("status")
    upd_adv = rec and rec.get("updated_at") != base_upd
    has_scores = bool(rec and rec.get("scores"))
    tag = []
    if new_run:
        tag.append(f"run={status} src={run.get('source')} model={run.get('model')} "
                   f"{(run.get('duration_ms') or 0) // 1000}s")
    else:
        tag.append("no new run yet")
    tag.append("scores✓" if has_scores else "scores=NULL")
    print(f"  … {age // 60}m{age % 60:02d}s  {' | '.join(tag)}")

    done = new_run and str(status).lower() in ("completed", "failed", "error") and upd_adv
    if done or (new_run and has_scores and upd_adv):
        run_seen = run
        break

print("\n" + "=" * 78)
final = dr()
run = run_seen or latest_run()
print(f"CANARY RESULT — {LABEL}")
print(f"  final: updated_at={str((final or {}).get('updated_at'))[:19]}  {scoreline(final)}")
if run:
    print(f"  run: status={run.get('status')} src={run.get('source')} model={run.get('model')} "
          f"dur={(run.get('duration_ms') or 0) // 1000}s tokens={run.get('total_tokens')} "
          f"violations={run.get('validation_violations')} err={run.get('error')}")

ds = (final or {}).get("scores")
model = str((run or {}).get("model") or "")
pass_scores = bool(ds)
pass_ai = bool(ds) and ds.get("factor_source") == "ai"
pass_model = "sonnet-5" in model or "sonnet5" in model
verdict = pass_scores and pass_ai and pass_model
print(f"\n  non-null deal_scores : {'PASS' if pass_scores else 'FAIL'}")
print(f"  factor_source == ai  : {'PASS' if pass_ai else 'FAIL'}  ({ds.get('factor_source') if ds else '-'})")
print(f"  model == sonnet-5    : {'PASS' if pass_model else 'FAIL'}  ({model or '-'})")
print(f"\n  >>> CANARY {'PASSED — safe to fan out the remaining 5' if verdict else 'FAILED — DO NOT fan out'} <<<")
sys.exit(0 if verdict else 1)
