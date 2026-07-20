"""LOCAL-ONLY rescore of all FORECASTED deals (forecast_critical = true). READS the DB, computes
fresh scores with the current local scoring code (qualification gate, at-risk-champion +
keyword-preference guards, symmetric momentum, decline-discounted engagement, near-term plan,
corrected forecast rank) — and writes NOTHING back. Output: a CSV with old/new scores + the
reasons, and a console summary. Push happens later via a separate --apply pass."""
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

OUT = "forecasted_rescore_2026-07-08.csv"
sec = load_secret(); SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}


def fix_ft_sign(ai):
    """Re-derive the stored forecast_category_trend sign with the corrected _FC_RANK (idempotent)."""
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
    """Compact 'factor +pts (evidence)' reason string from a score block's contributions."""
    out = []
    for c in sorted((block or {}).get("contributions") or [], key=lambda x: -abs(float(x.get("points") or 0))):
        p = float(c.get("points") or 0)
        f = str(c.get("factor") or "?")
        ev = re.sub(r"\s+", " ", str(c.get("evidence") or "")).strip()[:110]
        if abs(p) < 0.05 and f not in ("qualification_gate", "risk_note", "false_velocity"):
            continue
        out.append(f"{f} {p:+.1f}: {ev}" if ev else f"{f} {p:+.1f}")
        if len(out) >= cap:
            break
    return " | ".join(out)


rows = requests.get(f"{SB}/rest/v1/deal_records",
                    params={"select": "opp_id,account_name,opp_name,stage,forecast_category,amount,close_date,record",
                            "active": "eq.true", "forecast_critical": "eq.true", "limit": "200"},
                    headers=H, verify=VERIFY, timeout=180).json()
print(f"forecasted deals: {len(rows)}  (LOCAL compute only — nothing written)")

table = []
for r in sorted(rows, key=lambda x: -(float(x.get("amount") or 0))):
    rec = copy.deepcopy(r.get("record") or {}); ai = rec.get("ai") or {}
    old = (ai.get("deal_scores") or {}).get("headline") or {}
    pinned = bool((ai.get("deal_scores") or {}).get("pinned") or ai.get("pinned"))
    fix_ft_sign(ai)
    sc = SC.compute_deal_scores(rec)
    hl = sc.get("headline") or {}
    if hl.get("win_position") is None:
        table.append({"account": r.get("account_name"), "opp": r.get("opp_name"), "stage": r.get("stage"),
                      "fc": r.get("forecast_category"), "amount": r.get("amount"), "close": r.get("close_date"),
                      "pinned": pinned, "win_old": old.get("win_position"), "win_new": None,
                      "mom_old": old.get("deal_momentum"), "mom_new": None, "note": "no-score (dead/thin)"})
        continue
    wp = sc.get("win_position") or {}
    qc, qbox, qst = SC._qualification_ceiling(rec)
    table.append({
        "account": r.get("account_name"), "opp": r.get("opp_name"), "stage": r.get("stage"),
        "fc": r.get("forecast_category"), "amount": r.get("amount"), "close": r.get("close_date"),
        "pinned": pinned,
        "win_old": old.get("win_position"), "win_new": hl.get("win_position"),
        "mom_old": old.get("deal_momentum"), "mom_new": hl.get("deal_momentum"),
        "fc_conf": hl.get("forecast_confidence"), "risk": hl.get("deal_risk"),
        "commit": hl.get("customer_commitment"),
        "ceiling": wp.get("ceiling"), "qual_cap": (f"{int(qc)} by {qbox}={qst}" if qc < 100 else ""),
        "override": wp.get("selection_override"),
        "win_reasons": reasons(wp), "mom_reasons": reasons(sc.get("deal_momentum")),
        "note": "",
    })

with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=["account", "opp", "stage", "fc", "amount", "close", "pinned",
                                      "win_old", "win_new", "mom_old", "mom_new", "fc_conf", "risk",
                                      "commit", "ceiling", "qual_cap", "override",
                                      "win_reasons", "mom_reasons", "note"])
    w.writeheader()
    for t in table:
        w.writerow(t)
print(f"CSV written: {OUT}  ({len(table)} rows)\n")

def n(v):
    return v if isinstance(v, (int, float)) else float("nan")

moved = [t for t in table if isinstance(t.get("win_old"), (int, float)) and isinstance(t.get("win_new"), (int, float))
         and (abs(n(t["win_old"]) - n(t["win_new"])) >= 0.1 or abs(n(t.get("mom_old")) - n(t.get("mom_new"))) >= 0.1)]
print(f"deals whose scores CHANGE vs stored: {len(moved)} / {len(table)}")
print()
print(f"{'account':28} {'stage':16} {'fc':14} {'win':>12} {'mom':>12}  flags")
for t in table:
    wn = t.get("win_new"); wo = t.get("win_old"); mn = t.get("mom_new"); mo = t.get("mom_old")
    warr = f"{wo}->{wn}" if (isinstance(wo,(int,float)) and isinstance(wn,(int,float)) and abs(wo-wn)>=0.1) else f"{wn}"
    marr = f"{mo}->{mn}" if (isinstance(mo,(int,float)) and isinstance(mn,(int,float)) and abs(mo-mn)>=0.1) else f"{mn}"
    fl = ("PIN " if t.get("pinned") else "") + ("OVR " if t.get("override") else "") + (t.get("qual_cap") or "") + (t.get("note") or "")
    print(f"{str(t['account'])[:28]:28} {str(t['stage'])[:16]:16} {str(t['fc'])[:14]:14} {warr:>12} {marr:>12}  {fl[:36]}")
