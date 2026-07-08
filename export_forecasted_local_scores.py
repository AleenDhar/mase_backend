"""LOCAL-ONLY scoring run for all FORECASTED deals (forecast_critical=true).
Computes Win + Momentum with the CURRENT working-tree logic and builds the CRO-panel
REASONS (the same bullets the UI drawer shows). Writes a CSV — NO DB WRITES ANYWHERE.
"""
import sys, csv, copy, os
import requests, urllib3
import deal_engine_scoring as SC
import deal_engine_cro as CRO
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OUT = os.path.join(os.path.expanduser("~"), "Desktop", "forecasted_scores_local_v2.csv")

sec = load_secret(); base = sec["SUPABASE_URL"].rstrip("/")
key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": key, "Authorization": f"Bearer {key}"}
rows = requests.get(f"{base}/rest/v1/deal_records",
                    params={"select": "opp_id,account_name,opp_name,stage,forecast_category,amount,close_date,record",
                            "active": "eq.true", "forecast_critical": "eq.true",
                            "order": "amount.desc", "limit": "200"},
                    headers=H, verify=VERIFY, timeout=180).json()
print(f"forecasted deals: {len(rows)} — computing locally (no writes)")


def bullets(panel, key_):
    for b in (panel or {}).get("blocks") or []:
        if b.get("key") == key_:
            out = []
            for bl in b.get("bullets") or []:
                tone = "OK" if bl.get("tone") == "good" else "WARN"
                out.append(f"[{tone}] {bl.get('text') or ''}")
            return " || ".join(out), (b.get("summary") or b.get("read") or "")
    return "", ""


recs = []
for r in rows:
    rec = copy.deepcopy(r.get("record") or {})
    ai = rec.get("ai") or {}
    stored = (ai.get("deal_scores") or {}).get("headline") or {}
    pinned = bool((ai.get("deal_scores") or {}).get("pinned") or ai.get("pinned"))
    try:
        sc = SC.compute_deal_scores(rec)
        hl = sc.get("headline") or {}
        rec.setdefault("ai", {})["deal_scores"] = sc         # local object only — never persisted
        panel = CRO.build_cro_panel(rec) or {}
        win_r, win_s = bullets(panel, "win_position")
        mom_r, mom_s = bullets(panel, "deal_momentum")
        recs.append({
            "account": r.get("account_name"), "opportunity": r.get("opp_name"),
            "opp_id": r.get("opp_id"), "stage": r.get("stage"),
            "forecast_category": r.get("forecast_category"), "amount": r.get("amount"),
            "close_date": r.get("close_date"), "pinned": pinned,
            "win_local": hl.get("win_position"), "momentum_local": hl.get("deal_momentum"),
            "win_ui_current": stored.get("win_position"), "momentum_ui_current": stored.get("deal_momentum"),
            "win_read": win_s, "win_reasons": win_r,
            "momentum_read": mom_s, "momentum_reasons": mom_r,
        })
    except Exception as e:  # noqa: BLE001
        recs.append({"account": r.get("account_name"), "opp_id": r.get("opp_id"),
                     "stage": r.get("stage"), "win_reasons": f"ERROR: {e}"})

cols = ["account", "opportunity", "opp_id", "stage", "forecast_category", "amount", "close_date",
        "pinned", "win_local", "momentum_local", "win_ui_current", "momentum_ui_current",
        "win_read", "win_reasons", "momentum_read", "momentum_reasons"]
with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
    w.writeheader(); w.writerows(recs)
print(f"CSV: {OUT}  ({len(recs)} deals; NO DB writes performed)")
