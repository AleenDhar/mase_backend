"""LOCAL-ONLY rescore of ALL active deals with the current local scoring code (incl. the 3 new
fixes: verdict reconcile, blocked-differentiator guard, close-push ramp). READS the DB, writes
NOTHING back. Output: CSV with old/new scores, verdict, and full reasons."""
import sys, csv, re, copy
import requests, urllib3
import deal_engine_scoring as SC
import deal_engine_trends as T
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OUT = "all_opps_rescore_2026-07-08.csv"
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


def reasons(block, cap=6):
    out = []
    for c in sorted((block or {}).get("contributions") or [], key=lambda x: -abs(float(x.get("points") or 0))):
        p = float(c.get("points") or 0)
        f = str(c.get("factor") or "?")
        ev = re.sub(r"\s+", " ", str(c.get("evidence") or "")).strip()[:110]
        if abs(p) < 0.05 and f not in ("qualification_gate", "verdict_reconcile", "risk_note", "false_velocity"):
            continue
        out.append(f"{f} {p:+.1f}: {ev}" if ev else f"{f} {p:+.1f}")
        if len(out) >= cap:
            break
    return " | ".join(out)


rows = requests.get(f"{SB}/rest/v1/deal_records",
                    params={"select": "opp_id,account_name,opp_name,stage,forecast_category,forecast_critical,amount,close_date,record",
                            "active": "eq.true", "limit": "900"},
                    headers=H, verify=VERIFY, timeout=180).json()
print(f"active deals: {len(rows)}  (LOCAL compute only — nothing written)")

table = []
for r in sorted(rows, key=lambda x: -(float(x.get("amount") or 0))):
    rec = copy.deepcopy(r.get("record") or {}); ai = rec.get("ai") or {}
    old = (ai.get("deal_scores") or {}).get("headline") or {}
    pinned = bool((ai.get("deal_scores") or {}).get("pinned") or ai.get("pinned"))
    verd = ((ai.get("north_star_verdict") or {}).get("verdict")) or ""
    fix_ft_sign(ai)
    sc = SC.compute_deal_scores(rec)
    hl = sc.get("headline") or {}
    row = {"account": r.get("account_name"), "opp": r.get("opp_name"), "stage": r.get("stage"),
           "forecasted": bool(r.get("forecast_critical")), "fc": r.get("forecast_category"),
           "amount": r.get("amount"), "close": r.get("close_date"), "pinned": pinned, "verdict": verd,
           "win_old": old.get("win_position"), "win_new": hl.get("win_position"),
           "mom_old": old.get("deal_momentum"), "mom_new": hl.get("deal_momentum"),
           "fc_conf": hl.get("forecast_confidence"), "risk": hl.get("deal_risk"),
           "commit": hl.get("customer_commitment"), "note": ""}
    if hl.get("win_position") is None:
        row["note"] = sc.get("dead_label") or "no-score (dead/thin)"
    else:
        wp = sc.get("win_position") or {}
        qc, qbox, qst = SC._qualification_ceiling(rec)
        row["ceiling"] = wp.get("ceiling")
        row["qual_cap"] = (f"{int(qc)} by {qbox}={qst}" if qc < 100 else "")
        row["override"] = wp.get("selection_override")
        row["win_reasons"] = reasons(wp)
        row["mom_reasons"] = reasons(sc.get("deal_momentum"))
    table.append(row)

FIELDS = ["account", "opp", "stage", "forecasted", "fc", "amount", "close", "pinned", "verdict",
          "win_old", "win_new", "mom_old", "mom_new", "fc_conf", "risk", "commit",
          "ceiling", "qual_cap", "override", "win_reasons", "mom_reasons", "note"]
with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=FIELDS)
    w.writeheader()
    for t in table:
        w.writerow({k: t.get(k) for k in FIELDS})
print(f"CSV written: {OUT}  ({len(table)} rows)\n")

moved = []
for t in table:
    wo, wn, mo, mn = t.get("win_old"), t.get("win_new"), t.get("mom_old"), t.get("mom_new")
    dw = (wo - wn) if isinstance(wo, (int, float)) and isinstance(wn, (int, float)) else 0
    dm = (mo - mn) if isinstance(mo, (int, float)) and isinstance(mn, (int, float)) else 0
    if abs(dw) >= 0.1 or abs(dm) >= 0.1:
        moved.append((max(abs(dw), abs(dm)), dw, dm, t))
print(f"deals whose scores change vs stored: {len(moved)} / {len(table)}")
vr = sum(1 for _, _, dm, t in moved if dm > 0 and "verdict_reconcile" in str(t.get("mom_reasons")))
print(f"  of which momentum capped by VERDICT RECONCILE: {vr}")
print()
print(f"{'account':26} {'stage':14} {'verdict':14} {'win':>13} {'mom':>13}")
for _, dw, dm, t in sorted(moved, key=lambda x: -x[0])[:25]:
    wa = f"{t['win_old']}->{t['win_new']}" if abs(dw) >= 0.1 else f"{t['win_new']}"
    ma = f"{t['mom_old']}->{t['mom_new']}" if abs(dm) >= 0.1 else f"{t['mom_new']}"
    print(f"{str(t['account'])[:26]:26} {str(t['stage'])[:14]:14} {str(t['verdict'])[:14]:14} {wa:>13} {ma:>13}")
