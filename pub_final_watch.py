"""Watch Publicis's retry sweep until the queue settles; verify the final record. Read-only."""
import sys, time, json, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sec = load_secret(); SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
OID = "006P700000Xl06R"
st = {}
t0 = time.time()
while time.time() - t0 < 2400:
    q = requests.get(f"{SB}/rest/v1/sweep_queue",
                     params={"select": "status,attempts,error", "opp_id_15": f"eq.{OID}", "limit": "1"},
                     headers=H, verify=VERIFY, timeout=30).json()
    st = (q[0] if q else {})
    print(f"[pub] {int(time.time()-t0)}s queue={st.get('status')}/a{st.get('attempts')}", flush=True)
    if st.get("status") in ("done", "failed"):
        break
    time.sleep(75)
rec = requests.get(f"{SB}/rest/v1/deal_records", params={"select": "record,swept_at", "opp_id": f"eq.{OID}"},
                   headers=H, verify=VERIFY, timeout=60).json()[0]
ai = rec["record"].get("ai") or {}; ds = ai.get("deal_scores") or {}; hl = ds.get("headline") or {}
print("\n===== PUBLICIS FINAL (post-retry) =====")
print(f"queue: {st.get('status')} attempts={st.get('attempts')} err={str(st.get('error'))[:90]}")
print(f"WIN {hl.get('win_position')} | MOM {hl.get('deal_momentum')} | src={ds.get('factor_source')} | "
      f"degraded={ds.get('scoring_degraded')} ({str(ds.get('fallback_reason'))[:70]})")
print(f"panel blocks: {len((ds.get('cro_panel') or {}).get('blocks') or [])} | "
      f"ai_reasons win: {len((ds.get('ai_reasons') or {}).get('win_position') or [])}")
print(f"day_summary: {bool(ai.get('day_summary'))} | calls_read: {(rec['record'].get('evidence_coverage') or {}).get('calls_read')}")
print(f"provenance: {json.dumps((ai.get('scoring_studio') or {}).get('versions'))}")
