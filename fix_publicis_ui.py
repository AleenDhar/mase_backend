"""FIX THE UI DATA for Publicis: run a fresh dry-run sweep on the deployed (fixed) worker,
then write that correct record straight into deal_records — overwriting the scoreless husk
that vibe's admin/sweep 'Run Now' authored. Also clears the 'locally-authored' hold on the
queue row so the MASE worker will maintain it going forward.

This is a TARGETED, user-directed DB write (user: 'FIRST FIX THE DATA ON THE UI')."""
import sys, time, json, datetime, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OID = "006P700000Xl06R"
ENV = r"C:\Users\Aleen.Dhar\Desktop\MASE\frontend\.env.local"
cfg = {}
for line in open(ENV, encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); cfg[k.strip()] = v.strip()
API = cfg["DEAL_ENGINE_API_BASE"].rstrip("/")
AH = {"Authorization": f"Bearer {cfg['DEAL_ENGINE_TOKEN']}", "Content-Type": "application/json"}

sec = load_secret(); SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
HW = {**H, "Content-Type": "application/json", "Prefer": "return=representation"}
import os
DL = os.getenv("DATALAKE_URL")
if not DL:
    from daily_summary.common import load_datalake
    dl = load_datalake(); DL = dl["DATALAKE_URL"].rstrip("/"); DKEY = dl["DATALAKE_SERVICE_KEY"]
DH = {"apikey": DKEY, "Authorization": f"Bearer {DKEY}"}


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# 1) fire a fresh dry-run on the deployed fixed worker (no persist, no hold)
requests.delete(f"{DL}/rest/v1/ab_test_results", params={"opp_id": f"eq.{OID}"}, headers=DH, verify=VERIFY, timeout=30)
r = requests.post(f"{API}/api/deal-engine/sweep/{OID}/datalake-test", headers=AH,
                  json={"name": "Publicis - CLM and Request Management", "account": "Publicis Groupe"},
                  verify=False, timeout=60)
print("[fix] dry-run started:", r.status_code, r.text[:100], flush=True)

# 2) poll for the record
rec = None
t0 = time.time()
while time.time() - t0 < 1500:
    time.sleep(30)
    rows = requests.get(f"{DL}/rest/v1/ab_test_results",
                        params={"opp_id": f"eq.{OID}", "select": "status,error,result"},
                        headers=DH, verify=VERIFY, timeout=60).json()
    st = rows[0] if rows else {}
    print(f"[fix] {int(time.time()-t0)}s status={st.get('status')}", flush=True)
    if st.get("status") in ("completed", "failed"):
        rec = (st.get("result") or {}).get("record")
        break

if not isinstance(rec, dict):
    print("[fix] no record produced — ABORT (not writing)"); sys.exit(1)

ai = rec.get("ai") or {}; ds = ai.get("deal_scores") or {}; hl = ds.get("headline") or {}
print("\n[fix] fresh record scores:", hl.get("win_position"), "/", hl.get("deal_momentum"),
      "| src:", ds.get("factor_source"), "| degraded:", ds.get("scoring_degraded"),
      "| panel blocks:", len((ds.get("cro_panel") or {}).get("blocks") or []),
      "| day_summary:", bool(ai.get("day_summary")))
if hl.get("win_position") is None:
    print("[fix] fresh record STILL has no headline — ABORT (husk-floor should prevent this)"); sys.exit(1)

# 3) write the correct record straight into deal_records (overwrite the husk); flat mirror
#    columns stay as-is (they were already correct). Stamp swept_at/updated_at.
patch = {"record": rec, "swept_at": rec.get("swept_at") or now(), "updated_at": now(),
         "analysis_confidence": rec.get("analysis_confidence")}
w = requests.patch(f"{SB}/rest/v1/deal_records", params={"opp_id": f"eq.{OID}"}, headers=HW,
                   json=patch, verify=VERIFY, timeout=60)
print("[fix] deal_records overwrite:", w.status_code, "rows:", len(w.json() if w.text else []))

# 4) clear the vibe 'locally-authored' hold on the queue so the worker maintains it later
qw = requests.patch(f"{SB}/rest/v1/sweep_queue", params={"opp_id_15": f"eq.{OID}"}, headers=HW,
                    json={"status": "done", "error": None, "claimed_at": None, "updated_at": now()},
                    verify=VERIFY, timeout=60)
print("[fix] queue hold cleared:", qw.status_code)

# 5) verify the persisted record
v = requests.get(f"{SB}/rest/v1/deal_records", params={"select": "record,swept_at", "opp_id": f"eq.{OID}"},
                 headers=H, verify=VERIFY, timeout=60).json()[0]
vai = v["record"].get("ai") or {}; vds = vai.get("deal_scores") or {}; vhl = vds.get("headline") or {}
print("\n===== PUBLICIS UI DATA AFTER FIX =====")
print(f"WIN {vhl.get('win_position')} | MOM {vhl.get('deal_momentum')} | src={vds.get('factor_source')} "
      f"| degraded={vds.get('scoring_degraded')}")
print(f"reasons (cro_panel blocks): {len((vds.get('cro_panel') or {}).get('blocks') or [])} "
      f"| ai_reasons win: {len((vds.get('ai_reasons') or {}).get('win_position') or [])}")
print(f"24h day_summary: {bool(vai.get('day_summary'))}")
er = vai.get("explicit_requirements"); ern = len(er.get("items") or []) if isinstance(er, dict) else (len(er) if isinstance(er, list) else 0)
rm = vai.get("recommended_moves"); rmn = len(rm.get("items") or []) if isinstance(rm, dict) else (len(rm) if isinstance(rm, list) else 0)
print(f"todos: explicit_requirements={ern} recommended_moves={rmn}")
print(f"provenance: {json.dumps((vai.get('scoring_studio') or {}).get('versions'))}")
print("[fix] DONE — refresh the Publicis drawer")
