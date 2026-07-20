"""LOCAL-ONLY full-book recompute to quantify the DORMANT/ON-HOLD momentum floor
(2026-07-09, Galp). READS every active deal, recomputes scores with the current local
scorer (which now includes the dormant floor + the 3 prior local fixes), writes NOTHING
back. Output CSV flags every deal the dormant floor fires on and every score that moves."""
import sys, os, csv, re, copy, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
import deal_engine_scoring as SC
import deal_engine_trends as T
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OUT = "dormant_floor_rescore_2026-07-09.csv"
sec = load_secret(); SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}


def fix_ft_sign(ai):
    ot = ai.get("opp_trends") or {}
    v = ot.get("forecast_category_trend"); det = str(ot.get("forecast_category_trend_detail") or "")
    if not isinstance(v, (int, float)):
        return
    m = re.search(r"Forecast\s+(.+?)\s*->\s*(.+?)\s*\(", det)
    if not m:
        return
    ro = T._FC_RANK.get(m.group(1).strip().lower()); rn = T._FC_RANK.get(m.group(2).strip().lower())
    if ro is None or rn is None or ro == rn:
        return
    ot["forecast_category_trend"] = abs(v) * (1.0 if rn > ro else -1.0)
    ai["opp_trends"] = ot


rows = requests.get(f"{SB}/rest/v1/deal_records",
                    params={"select": "opp_id,account_name,opp_name,stage,forecast_category,amount,record",
                            "active": "eq.true", "limit": "900"},
                    headers=H, verify=VERIFY, timeout=180).json()
print(f"active deals: {len(rows)}  (LOCAL compute only — nothing written)")

table, dormant_hits, moved = [], 0, 0
for r in sorted(rows, key=lambda x: -(float(x.get("amount") or 0))):
    rec = copy.deepcopy(r.get("record") or {}); ai = rec.get("ai") or {}
    old = (ai.get("deal_scores") or {}).get("headline") or {}
    fix_ft_sign(ai)
    dorm, why = SC._dormant_read(rec)
    sc = SC.compute_deal_scores(rec)
    hl = sc.get("headline") or {}
    wo, wn = old.get("win_position"), hl.get("win_position")
    mo, mn = old.get("deal_momentum"), hl.get("deal_momentum")
    dw = (wo - wn) if isinstance(wo, (int, float)) and isinstance(wn, (int, float)) else 0
    dm = (mo - mn) if isinstance(mo, (int, float)) and isinstance(mn, (int, float)) else 0
    if dorm:
        dormant_hits += 1
    if abs(dw) >= 0.1 or abs(dm) >= 0.1:
        moved += 1
    table.append({
        "account": r.get("account_name"), "opp": r.get("opp_name"), "stage": r.get("stage"),
        "fc": r.get("forecast_category"), "amount": r.get("amount"),
        "dormant_floor": "YES" if dorm else "",
        "win_stored": wo, "win_new": wn, "mom_stored": mo, "mom_new": mn,
        "verdict": (ai.get("north_star_verdict") or {}).get("verdict"),
        "dormant_reason": why if dorm else "",
    })

FIELDS = ["account", "opp", "stage", "fc", "amount", "dormant_floor",
          "win_stored", "win_new", "mom_stored", "mom_new", "verdict", "dormant_reason"]
with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=FIELDS)
    w.writeheader()
    for t in table:
        w.writerow(t)

print(f"CSV: {os.path.abspath(OUT)}  ({len(table)} rows)")
print(f"dormant floor fires on: {dormant_hits} deals | total deals whose scores move vs stored: {moved}\n")
print("=== DORMANT-FLOORED DEALS ===")
print(f"{'account':30} {'stage':16} {'fc':10} {'win s->n':>12} {'mom s->n':>12}")
for t in sorted([t for t in table if t["dormant_floor"]], key=lambda x: -(x["mom_stored"] or 0)):
    print(f"{str(t['account'])[:30]:30} {str(t['stage'])[:16]:16} {str(t['fc'])[:10]:10} "
          f"{str(t['win_stored'])+'->'+str(t['win_new']):>12} {str(t['mom_stored'])+'->'+str(t['mom_new']):>12}")
