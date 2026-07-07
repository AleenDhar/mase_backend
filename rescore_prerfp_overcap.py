"""Rescore every pre-RFP deal (Initial Interest / Qualified / discovery / prospect) whose stored
win reads above the 30 pre-RFP ceiling — the selection-override-before-evaluation bug (PremiStar
Qualified read 99). The override stage-gate fix now blocks these; this re-applies the correct
score + rebuilds the CRO panel to match. Pinned deals skipped. --apply writes; else dry-run."""
import sys, json, importlib
import requests, urllib3
import deal_engine_scoring as SC; importlib.reload(SC)
import deal_engine_cro as CRO
from daily_summary.common import load_secret, VERIFY
import re
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APPLY = "--apply" in sys.argv
sec = load_secret(); SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
REF = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
MGMT = f"https://api.supabase.com/v1/projects/{REF}/database/query"; MTOK = sec["SUPABASE_ACCESS_TOKEN"]
PRE = ("initial interest", "qualified", "prospect", "discovery", "lead")

rows = requests.get(f"{SB}/rest/v1/deal_records", params={"select": "opp_id,account_name,stage,record", "active": "eq.true", "limit": "600"},
                    headers=H, verify=VERIFY, timeout=180).json()
ds_out, panel_out, report = {}, {}, []
for r in rows:
    st = str(r.get("stage") or "").lower()
    rec = r.get("record") or {}; ai = rec.get("ai") or {}
    hl = (ai.get("deal_scores") or {}).get("headline") or {}
    w = hl.get("win_position")
    if not any(t in st for t in PRE):
        continue
    if not (isinstance(w, (int, float)) and w > 30):
        continue
    if (ai.get("deal_scores") or {}).get("pinned") or ai.get("pinned"):
        report.append((r.get("account_name"), st, w, "PINNED-skip")); continue
    oid = r["opp_id"]
    sc = SC.compute_deal_scores(rec)
    nw = (sc.get("headline") or {}).get("win_position")
    if nw is None:
        report.append((r.get("account_name"), st, w, "no-score-skip")); continue
    rec.setdefault("ai", {})["deal_scores"] = sc
    p = CRO.build_cro_panel(rec)
    if p:
        sc["cro_panel"] = p
    ds_out[oid] = sc
    report.append((r.get("account_name"), st, w, f"-> {nw}"))

print(f"pre-RFP over-cap deals: {len(ds_out)}")
for nm, st, w, res in report:
    print(f"  {str(nm)[:30]:30} {st:20} {w:>5} {res}")

if not APPLY:
    print("\n[DRY RUN] --apply to write."); sys.exit()

items = list(ds_out.items()); n = 0
for i in range(0, len(items), 50):
    blob = json.dumps(dict(items[i:i + 50]))
    sql = ("update deal_records d set record = jsonb_set(record,'{ai,deal_scores}', m.value, true), updated_at=now() "
           "from (select key as opp_id, value from jsonb_each($J$" + blob + "$J$::jsonb)) m where d.opp_id=m.opp_id returning d.opp_id")
    resp = requests.post(MGMT, headers={"Authorization": f"Bearer {MTOK}", "Content-Type": "application/json"},
                         json={"query": sql}, verify=VERIFY, timeout=120)
    n += len(resp.json()) if resp.status_code < 300 else 0
print(f"applied deal_scores: {n}")
