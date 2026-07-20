"""FAST: (1) fix the Publicis husk IN PLACE by computing scores + CRO panel on its EXISTING
rich record (no sweep needed — the record already has meddpicc/stakeholders/moves, just no
scores), and (2) HALT the queue so the worker stops processing the automated backlog now.
User-directed emergency fix."""
import sys, os, json, datetime, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sec = load_secret()
for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_KEY"):
    if sec.get(k):
        os.environ[k] = sec[k]
os.environ.setdefault("DEAL_ENGINE_AI_SCORING", "")   # deterministic compute here (instant, no LLM)
SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
HW = {**H, "Content-Type": "application/json", "Prefer": "return=representation"}


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


OID = "006P700000Xl06R"
rec = requests.get(f"{SB}/rest/v1/deal_records", params={"select": "record", "opp_id": f"eq.{OID}"},
                   headers=H, verify=VERIFY, timeout=60).json()[0]["record"]
ai = rec.get("ai") or {}
print("Publicis record ai keys:", len(ai.keys()), "| has meddpicc:", bool(ai.get("meddpicc")),
      "| has moves:", bool(ai.get("recommended_moves")))

import deal_engine_scoring as SC
import deal_engine_cro as CRO
sc = SC.compute_deal_scores(rec)
hl = (sc or {}).get("headline") or {}
if hl.get("win_position") is None:
    print("compute_deal_scores produced no headline — ABORT"); sys.exit(1)
sc["scoring_degraded"] = True
sc["fallback_reason"] = "record repaired in place from stored analysis (0-call sweep husk)"
rec.setdefault("ai", {})["deal_scores"] = sc
try:
    panel = CRO.build_cro_panel(rec)
    if panel:
        rec["ai"]["deal_scores"]["cro_panel"] = panel
except Exception as e:
    print("cro_panel build skipped:", e)
rec["swept_at"] = now()
print(f"computed: WIN {hl.get('win_position')} MOM {hl.get('deal_momentum')} | "
      f"panel blocks {len((rec['ai']['deal_scores'].get('cro_panel') or {}).get('blocks') or [])}")

w = requests.patch(f"{SB}/rest/v1/deal_records", params={"opp_id": f"eq.{OID}"}, headers=HW,
                   json={"record": rec, "swept_at": rec["swept_at"], "updated_at": now()},
                   verify=VERIFY, timeout=60)
print("Publicis record repaired:", w.status_code, "rows:", len(w.json() if w.text else []))

# ---- HALT the queue: mark every waiting/working row done so the worker stops processing ----
halted = requests.patch(f"{SB}/rest/v1/sweep_queue", params={"status": "in.(waiting,working)"},
                        headers=HW, json={"status": "done", "claimed_at": None,
                                          "error": "halted: automated sweeping paused (manual-only)",
                                          "updated_at": now()}, verify=VERIFY, timeout=60)
n = len(halted.json()) if halted.text else 0
print(f"queue HALTED: {halted.status_code} — {n} waiting/working row(s) -> done")
