"""LOCAL-ONLY calibration-set rescore. Recomputes BOTH scores (win + momentum) with the
current local scorer (all 4 local fixes incl. the dormant floor) for the requested live opps
plus the calibration anchors already tested, and writes a CSV with the FULL reason breakdown
(every contribution: factor, points, evidence) for BOTH scores. READS the DB, writes NOTHING."""
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

OUT = "calibration_set_rescore_2026-07-09.csv"
sec = load_secret(); SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}

# The 5 the user asked for, in requested order, then the anchors already tested (calibration spread).
REQUESTED = ["Consumer Cellular", "Publicis", "Austrian Post", "John Deere", "Temasek"]
ANCHORS = ["Galp Energia", "Alghanim"]


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


def reasons(block, cap=14):
    """FULL 'factor +pts: evidence' breakdown — keep structural zero-point factors too."""
    keep0 = ("qualification_gate", "verdict_reconcile", "dormant_floor", "risk_note",
             "false_velocity", "cadence", "selection_override", "process_floor")
    out = []
    for c in sorted((block or {}).get("contributions") or [], key=lambda x: -abs(float(x.get("points") or 0))):
        p = float(c.get("points") or 0)
        f = str(c.get("factor") or "?")
        ev = re.sub(r"\s+", " ", str(c.get("evidence") or "")).strip()[:150]
        if abs(p) < 0.05 and f not in keep0:
            continue
        out.append(f"{f} {p:+.1f}: {ev}" if ev else f"{f} {p:+.1f}")
        if len(out) >= cap:
            break
    return "\n".join(out)


def joinlines(seq):
    out = []
    for x in seq or []:
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            out.append(str(next((x[k] for k in ("text", "reason", "bullet", "point", "detail") if x.get(k)),
                                str(x))))
    return "\n".join(out)


def win_band(s):
    if not isinstance(s, (int, float)): return ""
    return ("Winning" if s >= 85 else "Strong" if s >= 70 else "In the fight" if s >= 45
            else "Behind/early" if s >= 25 else "Weak")


def mom_band(s):
    if not isinstance(s, (int, float)): return ""
    return ("Accelerating" if s >= 80 else "Healthy/building" if s >= 60 else "Steady" if s >= 45
            else "Flat" if s >= 35 else "Slowing/stalled")


def fetch(name):
    return requests.get(f"{SB}/rest/v1/deal_records",
                        params={"select": "opp_id,account_name,opp_name,stage,forecast_category,amount,close_date,record",
                                "account_name": f"ilike.*{name}*", "active": "eq.true", "limit": "1"},
                        headers=H, verify=VERIFY, timeout=60).json()


rows_out = []
for group, names in (("requested", REQUESTED), ("anchor", ANCHORS)):
    for name in names:
        rr = fetch(name)
        if not rr:
            print(f"{name}: not found"); continue
        r = rr[0]
        rec = copy.deepcopy(r.get("record") or {}); ai = rec.get("ai") or {}
        old = (ai.get("deal_scores") or {}).get("headline") or {}
        fix_ft_sign(ai)
        dorm, dorm_why = SC._dormant_read(rec)
        sc = SC.compute_deal_scores(rec)
        hl = sc.get("headline") or {}
        wp = sc.get("win_position") or {}; mm = sc.get("deal_momentum") or {}
        qc, qb, qs = SC._qualification_ceiling(rec)
        ev = ai.get("deal_scores_evidence") or {}; air = ev.get("ai_reasons") or {}
        wn, mn = hl.get("win_position"), hl.get("deal_momentum")
        rows_out.append({
            "set": group, "account": r.get("account_name"), "opp": r.get("opp_name"),
            "stage": r.get("stage"), "forecast": r.get("forecast_category"),
            "amount": r.get("amount"), "close": r.get("close_date"),
            "verdict": (ai.get("north_star_verdict") or {}).get("verdict"),
            "win_stored": old.get("win_position"), "win_new": wn, "win_band": win_band(wn),
            "win_ceiling": wp.get("ceiling"), "qual_cap": (f"{int(qc)} by {qb}={qs}" if qc < 100 else ""),
            "selection_override": wp.get("selection_override"),
            "mom_stored": old.get("deal_momentum"), "mom_new": mn, "mom_band": mom_band(mn),
            "dormant_floor": "YES" if dorm else "",
            "fc_confidence": hl.get("forecast_confidence"), "risk": hl.get("deal_risk"),
            "commitment": hl.get("customer_commitment"),
            "WIN_reasons": reasons(wp),
            "MOMENTUM_reasons": reasons(mm),
            "ai_win_narrative": joinlines(air.get("win_position")),
            "ai_momentum_narrative": joinlines(air.get("deal_momentum")),
        })

FIELDS = ["set", "account", "opp", "stage", "forecast", "amount", "close", "verdict",
          "win_stored", "win_new", "win_band", "win_ceiling", "qual_cap", "selection_override",
          "mom_stored", "mom_new", "mom_band", "dormant_floor",
          "fc_confidence", "risk", "commitment",
          "WIN_reasons", "MOMENTUM_reasons", "ai_win_narrative", "ai_momentum_narrative"]
with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=FIELDS)
    w.writeheader()
    for t in rows_out:
        w.writerow(t)

print(f"CSV: {os.path.abspath(OUT)}  ({len(rows_out)} rows)  — nothing written to any DB\n")
print(f"{'account':26} {'stage':16} {'win s->n (band)':>26} {'mom s->n (band)':>26} {'verdict':10} dorm")
for t in rows_out:
    ws = f"{t['win_stored']}->{t['win_new']} ({t['win_band']})"
    ms = f"{t['mom_stored']}->{t['mom_new']} ({t['mom_band']})"
    print(f"{str(t['account'])[:26]:26} {str(t['stage'])[:16]:16} {ws:>26} {ms:>26} {str(t['verdict'])[:10]:10} {t['dormant_floor']}")
