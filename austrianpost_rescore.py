"""Update Austrian Post's PINNED deal_scores to the honest recompute (momentum fix: 99->~90),
keeping pinned=true and rebuilding the CRO panel to match. --apply writes."""
import sys, json, re, copy
import requests, urllib3
import deal_engine_scoring as SC, deal_engine_cro as CRO, deal_engine_trends as T
from daily_summary.common import load_secret, VERIFY


def fix_ft_sign(ai):
    ot = ai.get("opp_trends") or {}
    v = ot.get("forecast_category_trend"); det = str(ot.get("forecast_category_trend_detail") or "")
    if not isinstance(v, (int, float)):
        return False
    m = re.search(r"Forecast\s+(.+?)\s*->\s*(.+?)\s*\(", det)
    if not m:
        return False
    ro = T._FC_RANK.get(m.group(1).strip().lower()); rn = T._FC_RANK.get(m.group(2).strip().lower())
    if ro is None or rn is None or ro == rn:
        return False
    ot["forecast_category_trend"] = abs(v) * (1.0 if rn > ro else -1.0)
    ai["opp_trends"] = ot
    return True
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APPLY = "--apply" in sys.argv
sec = load_secret(); base = sec["SUPABASE_URL"].rstrip("/")
key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": key, "Authorization": f"Bearer {key}"}
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
mgmt = f"https://api.supabase.com/v1/projects/{ref}/database/query"; tok = sec["SUPABASE_ACCESS_TOKEN"]

r = requests.get(f"{base}/rest/v1/deal_records",
                 params={"account_name": "ilike.*Austrian*Post*", "active": "eq.true",
                         "select": "opp_id,record"}, headers=H, verify=VERIFY, timeout=40).json()[0]
oid = r["opp_id"]; rec = r["record"]; ai = rec.get("ai") or {}
old = (ai.get("deal_scores") or {}).get("headline") or {}
was_pinned = bool((ai.get("deal_scores") or {}).get("pinned") or ai.get("pinned"))
print(f"opp_id={oid} pinned={was_pinned}")
print(f"OLD: win={old.get('win_position')} mom={old.get('deal_momentum')}")

ft_changed = fix_ft_sign(ai)   # re-derive forecast_category_trend sign with corrected _FC_RANK
print(f"forecast-sign corrected: {ft_changed} -> {(ai.get('opp_trends') or {}).get('forecast_category_trend')}")
sc = SC.compute_deal_scores(copy.deepcopy(rec))
new = sc.get("headline") or {}
print(f"NEW: win={new.get('win_position')} mom={new.get('deal_momentum')}")
# preserve the pin on the fresh scores + rebuild panel
sc["pinned"] = True
rec.setdefault("ai", {})["deal_scores"] = sc
panel = CRO.build_cro_panel(rec)
if panel:
    sc["cro_panel"] = panel

if not APPLY:
    print("\n[DRY RUN] --apply to write."); sys.exit()

ds_blob = json.dumps(sc, ensure_ascii=False)
ot_blob = json.dumps(ai.get("opp_trends") or {}, ensure_ascii=False)
sql = ("update deal_records set record = jsonb_set(jsonb_set(record,'{ai,opp_trends}', $O$" + ot_blob +
       "$O$::jsonb, true), '{ai,deal_scores}', $J$" + ds_blob + "$J$::jsonb, true), updated_at=now() "
       "where opp_id='" + oid + "' returning opp_id")
resp = requests.post(mgmt, headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                     json={"query": sql}, verify=VERIFY, timeout=90)
print("APPLIED:", resp.json() if resp.status_code < 300 else (resp.status_code, resp.text[:200]))
