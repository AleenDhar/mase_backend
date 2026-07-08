"""Rescore the book after the forecast-category-order + symmetric-momentum fix. The
forecast_category_trend sign is BAKED into each record's ai.opp_trends (computed at sweep time with
the old wrong rank), so first RE-DERIVE its sign from its detail string using the corrected
_FC_RANK, then recompute deal_scores + panel. Persists both ai.opp_trends and ai.deal_scores.
Skips pinned (handled separately). --apply writes; else dry-run summary."""
import sys, re, json, copy
import requests, urllib3
import deal_engine_scoring as SC, deal_engine_cro as CRO, deal_engine_trends as T
from daily_summary.common import load_secret, VERIFY
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


def fix_ft_sign(ai):
    """Re-derive forecast_category_trend sign from its detail with the corrected _FC_RANK.
    Returns True if it changed the stored value."""
    ot = ai.get("opp_trends") or {}
    v = ot.get("forecast_category_trend")
    det = str(ot.get("forecast_category_trend_detail") or "")
    if not isinstance(v, (int, float)):
        return False
    m = re.search(r"Forecast\s+(.+?)\s*->\s*(.+?)\s*\(", det)
    if not m:
        return False
    ro = T._FC_RANK.get(m.group(1).strip().lower())
    rn = T._FC_RANK.get(m.group(2).strip().lower())
    if ro is None or rn is None or ro == rn:
        return False
    corrected = abs(v) * (1.0 if rn > ro else -1.0)
    if abs(corrected - v) < 1e-9:
        return False
    ot["forecast_category_trend"] = corrected
    ai["opp_trends"] = ot
    return True


rows = requests.get(f"{base}/rest/v1/deal_records",
                    params={"select": "opp_id,account_name,record", "active": "eq.true", "limit": "800"},
                    headers=H, verify=VERIFY, timeout=180).json()
out_ds, out_ot, moved = {}, {}, []
for r in rows:
    rec = copy.deepcopy(r.get("record") or {}); ai = rec.get("ai") or {}
    if (ai.get("deal_scores") or {}).get("pinned") or ai.get("pinned"):
        continue
    old_m = (ai.get("deal_scores") or {}).get("headline", {}).get("deal_momentum")
    changed = fix_ft_sign(ai)
    sc = SC.compute_deal_scores(rec)
    if not sc or (sc.get("headline") or {}).get("win_position") is None:
        continue
    rec.setdefault("ai", {})["deal_scores"] = sc
    p = CRO.build_cro_panel(rec)
    if p:
        sc["cro_panel"] = p
    out_ds[r["opp_id"]] = sc
    if changed:
        out_ot[r["opp_id"]] = ai["opp_trends"]
    new_m = (sc.get("headline") or {}).get("deal_momentum")
    if isinstance(old_m, (int, float)) and isinstance(new_m, (int, float)) and abs(old_m - new_m) >= 0.1:
        moved.append((round(old_m - new_m, 1), str(r.get("account_name"))[:26], old_m, new_m))

pos = [m for m in moved if m[0] > 0]
print(f"deals rescored: {len(out_ds)} | forecast-sign corrected: {len(out_ot)} | momentum moved: {len(moved)} (drops {len(pos)})")
for d, nm, o, n in sorted(pos, reverse=True)[:12]:
    print(f"  -{d:<5} {nm:26} {o} -> {n}")
if not APPLY:
    print("\n[DRY RUN] --apply to write."); sys.exit()

# write deal_scores (+ corrected opp_trends where changed), chunked
items = list(out_ds.items()); n = 0
for i in range(0, len(items), 40):
    chunk = dict(items[i:i + 40])
    blob = json.dumps(chunk, ensure_ascii=False)
    ot_chunk = {k: out_ot[k] for k in chunk if k in out_ot}
    ot_blob = json.dumps(ot_chunk, ensure_ascii=False)
    sql = (
        "update deal_records d set record = jsonb_set("
        "  case when o.value is not null then jsonb_set(d.record,'{ai,opp_trends}', o.value, true) else d.record end,"
        "  '{ai,deal_scores}', m.value, true), updated_at = now() "
        "from (select key as opp_id, value from jsonb_each($J$" + blob + "$J$::jsonb)) m "
        "left join (select key as opp_id, value from jsonb_each($O$" + ot_blob + "$O$::jsonb)) o on o.opp_id = m.opp_id "
        "where d.opp_id = m.opp_id returning d.opp_id")
    resp = requests.post(mgmt, headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                         json={"query": sql}, verify=VERIFY, timeout=150)
    if resp.status_code >= 300:
        print("APPLY FAILED", resp.status_code, resp.text[:300]); break
    n += len(resp.json())
print(f"\nAPPLIED: {n} deals rescored (+forecast sign corrected)")
