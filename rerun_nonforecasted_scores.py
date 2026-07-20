"""Rerun ONLY the deterministic deal SCORING (compute_deal_scores + CRO panel) for every
NON-forecasted opp (forecast_critical = False) — NO LLM sweep. Forecasted opps (forecast_critical
= True, 69) are EXCLUDED: they were just freshly swept, so an offline recompute would overwrite
their fresh sweep scores. Pinned deals skipped. Applies the qualification-gated win to the long
tail. Dry-run by default; --apply writes."""
import sys, json, re
import requests, urllib3
import deal_engine_scoring as SC
import deal_engine_cro as CRO
from daily_summary.common import load_secret, VERIFY, id15
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


def _has_analysis(ai):
    return bool(ai.get("meddpicc") or ai.get("competitive_position") or ai.get("champion_strength") or ai.get("recommended_moves"))


rows = requests.get(f"{SB}/rest/v1/deal_records",
                    params={"select": "opp_id,account_name,stage,forecast_critical,record", "active": "eq.true", "limit": "700"},
                    headers=H, verify=VERIFY, timeout=180).json()

out = {}; skip_fc = skip_pin = skip_noan = skip_noscore = changed = 0; moves = []; breach = []
for r in rows:
    if r.get("forecast_critical"):
        skip_fc += 1; continue                        # forecasted -> leave the fresh sweep score
    rec = r.get("record") or {}; ai = rec.get("ai") or {}
    if ai.get("pinned") or (ai.get("deal_scores") or {}).get("pinned"):
        skip_pin += 1; continue
    if not _has_analysis(ai):
        skip_noan += 1; continue
    old = (ai.get("deal_scores") or {}).get("headline", {}).get("win_position")
    sc = SC.compute_deal_scores(rec)
    new = (sc.get("headline") or {}).get("win_position")
    if new is None:
        skip_noscore += 1; continue
    rec.setdefault("ai", {})["deal_scores"] = sc
    p = CRO.build_cro_panel(rec)
    if p:
        sc["cro_panel"] = p
    out[id15(r["opp_id"])] = sc
    wp = sc.get("win_position") or {}
    if wp.get("selection_override") and new is not None and float(new) > float(wp.get("ceiling") or 100):
        breach.append((r.get("account_name"), new))   # override breaching stage ceiling (should be none)
    if isinstance(old, (int, float)) and isinstance(new, (int, float)) and abs(old - new) >= 0.1:
        changed += 1
        if abs(old - new) >= 5:
            moves.append((round(new - old, 1), str(r.get("account_name"))[:26], str(r.get("stage"))[:16], old, new))

print(f"non-forecasted rescore: {len(out)} deals | changed {changed} | skip forecasted {skip_fc} | "
      f"skip pinned {skip_pin} | skip no-analysis {skip_noan} | skip no-score {skip_noscore}")
if breach:
    print(f"  !! {len(breach)} override deals breaching stage ceiling:", breach[:5])
print("  biggest score moves (>=5):")
for d, nm, st, o, n in sorted(moves, key=lambda x: x[0])[:12]:
    print(f"    {d:+6}  {nm:26} {st:16} {o} -> {n}")

if not APPLY:
    print("\n[DRY RUN] --apply to write."); sys.exit()
items = list(out.items()); n = 0
for i in range(0, len(items), 40):
    blob = json.dumps(dict(items[i:i + 40]))
    sql = ("update deal_records d set record = jsonb_set(record,'{ai,deal_scores}', m.value, true), updated_at=now() "
           "from (select key as opp_id, value from jsonb_each($J$" + blob + "$J$::jsonb)) m where d.opp_id=m.opp_id returning d.opp_id")
    resp = requests.post(MGMT, headers={"Authorization": f"Bearer {MTOK}", "Content-Type": "application/json"},
                         json={"query": sql}, verify=VERIFY, timeout=150)
    n += len(resp.json()) if resp.status_code < 300 else 0
print(f"APPLIED: {n} non-forecasted deal scores rerun")
