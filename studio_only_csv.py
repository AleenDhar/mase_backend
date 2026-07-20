"""LOCAL-ONLY CSV from the STUDIO-PROMPT-ONLY scoring runs (cc_work/<opp>.studioonly.json).
Scores are produced by the LLM applying ONLY the locked Scoring Version Studio engines
(system_prompt_studio_only.md) — NO base sweep prompt, NO deterministic Python scorer.

Per the user's instruction this CSV carries STUDIO SCORES ONLY — no deterministic, no stored.
Stage / forecast / amount / close are deal metadata (not scores) kept for context. Writes
NOTHING back to any DB."""
import sys, os, csv, json, warnings
warnings.filterwarnings("ignore")
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cc_work")
OUT = "studio_only_scores_2026-07-09.csv"
sec = load_secret(); SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}

# 5 live calibration deals + 5 dormant/frozen deals = 10, in this order.
OPPS = [("006P700000OcxpH", "Consumer Cellular"), ("006P700000Xl06R", "Publicis Groupe"),
        ("006P700000J71MD", "Austrian Post"), ("006P700000KHd9V", "John Deere"),
        ("006P700000BV2eA", "Temasek"),
        ("006P700000AfJyb", "Galp Energia"), ("006P700000KTTO5", "MTR Corporation Limited"),
        ("006P700000KlsBE", "SGD Pharma"), ("006P700000FF6Np", "A.R.M. Holding"),
        ("0066700000yP7lZ", "FGV Holdings Berhad")]


def jl(seq):
    out = []
    for x in seq or []:
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            out.append(str(next((x[k] for k in ("item", "evidence", "text", "reason", "driver", "detail")
                                 if x.get(k)), json.dumps(x, default=str))))
    return "\n".join(out)


def fmt_todo(sec_list, *keys):
    out = []
    for it in sec_list or []:
        if isinstance(it, str):
            out.append(it); continue
        txt = next((it.get(k) for k in keys if it.get(k)), None) or json.dumps(it, default=str)[:80]
        meta = " ".join(f"{k}={it[k]}" for k in ("date", "due", "who", "who_asked", "committed_on", "needed_by")
                        if it.get(k))
        out.append(txt + (f"  [{meta}]" if meta else ""))
    return "\n".join(out)


def meta(oid):
    """Deal metadata (NOT scores) for context: stage / forecast / amount / close."""
    try:
        rec = requests.get(f"{SB}/rest/v1/deal_records",
                           params={"select": "stage,forecast_category,amount,close_date", "opp_id": f"eq.{oid}"},
                           headers=H, verify=VERIFY, timeout=60).json()
        if rec:
            r = rec[0]
            return r.get("stage"), r.get("forecast_category"), r.get("amount"), r.get("close_date")
    except Exception:
        pass
    return None, None, None, None


rows_out = []
for oid, name in OPPS:
    sp = os.path.join(WORK, f"{oid}.studioonly.json")
    if not os.path.exists(sp):
        print(f"{name}: {os.path.basename(sp)} NOT READY — skip")
        continue
    try:
        d = json.load(open(sp, encoding="utf-8"))
    except Exception as e:
        print(f"{name}: parse fail {e}"); continue
    win = d.get("win") or {}; mom = d.get("mom") or {}; ext = d.get("extract") or {}
    tdo = d.get("todo") or {}; smm = d.get("sum") or {}
    stage, fc, amount, close = meta(oid)
    rows_out.append({
        "account": name, "stage": stage, "forecast": fc, "amount": amount, "close": close,
        "studio_win": win.get("score"), "studio_win_band": win.get("band"),
        "studio_momentum": mom.get("score"), "studio_momentum_band": mom.get("band"),
        "win_drivers": jl(win.get("drivers")),
        "win_focus_now": win.get("focus_now"), "win_ceiling_applied": win.get("ceiling_applied"),
        "win_exception_statement": win.get("exception_statement"),
        "momentum_drivers": jl(mom.get("drivers")), "momentum_focus_now": mom.get("focus_now"),
        "signal_extraction": jl(ext.get("signals")) + (f"\ncoverage={ext.get('coverage')}" if ext.get("coverage") else ""),
        "extraction_rationale": jl(ext.get("top_rationale")),
        "todo_prospect_requirements": fmt_todo(tdo.get("prospect_requirements"), "item", "requirement"),
        "todo_zycus_commitments": fmt_todo(tdo.get("zycus_commitments"), "item", "commitment"),
        "todo_waiting_on_buyer": fmt_todo(tdo.get("waiting_on_buyer"), "item"),
        "todo_best_practices": fmt_todo(tdo.get("best_practices"), "item"),
        "suggested_realistic_close": tdo.get("suggested_realistic_close"),
        "summary_24h": (smm.get("headline") or "") + "".join("\n" + s for s in (smm.get("supporting") or []))
                       + (f"\n({smm.get('as_of_note')})" if smm.get("as_of_note") else ""),
    })
    print(f"{name:26} STUDIO win={win.get('score')} ({win.get('band')})  momentum={mom.get('score')} ({mom.get('band')})")

if rows_out:
    fields = list(rows_out[0].keys())
    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows_out:
            w.writerow(r)
    print(f"\nCSV: {os.path.abspath(OUT)}  ({len(rows_out)} rows)  — STUDIO scores only; nothing written to any DB")
else:
    print("no studio-only outputs ready yet")
