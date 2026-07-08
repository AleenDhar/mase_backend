"""EVAL analysis — join baseline (v2 local) + eval_strict + eval_loose, compute
deltas + a robustness verdict per deal, print the CRO table, save eval_comparison.csv.
Momentum is deterministic (identical under both variants by design), so variant
sensitivity is judged on WIN (the AI-read MEDDPICC/preference/champion feed it)."""
import csv, os, sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DESK = os.path.join(os.path.expanduser("~"), "Desktop")


def num(v):
    try:
        return float(v) if v not in (None, "", "None") else None
    except ValueError:
        return None


def load(path, kw, km):
    out = {}
    for r in csv.DictReader(open(path, encoding="utf-8-sig")):
        out[r["opp_id"]] = {"win": num(r.get(kw)), "mom": num(r.get(km)), "row": r}
    return out


base = load(os.path.join(DESK, "forecasted_scores_local_v2.csv"), "win_local", "momentum_local")
base.setdefault("006P700000AfJyb", {"win": 19.5, "mom": 46.0,
                                    "row": {"account": "Galp Energia", "stage": "Qualified",
                                            "forecast_category": "Pipeline"}})
strict = load(os.path.join(DESK, "eval_strict.csv"), "win", "momentum")
loose = load(os.path.join(DESK, "eval_loose.csv"), "win", "momentum")

rows = []
for oid, s in strict.items():
    b, l = base.get(oid), loose.get(oid)
    if not (b and l):
        continue
    acc = s["row"].get("account") or b["row"].get("account")
    sw, lw, bw = s["win"], l["win"], b["win"]
    sm, lm, bm = s["mom"], l["mom"], b["mom"]
    stolen = []
    if sw is None:
        stolen.append("strict")
    if lw is None:
        stolen.append("loose")
    if stolen:
        verdict = f"NO CLEAN READ — rogue consumer stole the {'+'.join(stolen)} sweep(s)"
        win_spread = None
    else:
        win_spread = round(lw - sw, 1)          # loose minus strict (positive = looseness inflates)
        mom_data_move = round(sm - bm, 1)       # baseline→eval move = fresh DATA, not instruction
        if abs(win_spread) <= 5 and abs(sw - bw) <= 8:
            verdict = "ROBUST — evidence-backed under both readings"
        elif abs(win_spread) > 8:
            verdict = "READING-SENSITIVE — evidence too thin; win swings with the instruction"
        elif abs(win_spread) > 5:
            verdict = "MODERATELY reading-sensitive"
        else:
            verdict = "ROBUST vs reading; moved on fresh data vs baseline"
    rows.append({
        "account": acc, "opp_id": oid,
        "stage": s["row"].get("stage") or b["row"].get("stage"),
        "forecast_category": s["row"].get("forecast_category") or b["row"].get("forecast_category"),
        "win_base": bw, "win_strict": sw, "win_loose": lw,
        "mom_base": bm, "mom_strict": sm, "mom_loose": lm,
        "win_spread_loose_minus_strict": win_spread,
        "mom_move_vs_baseline_fresh_data": (round(sm - bm, 1) if (sm is not None and bm is not None) else None),
        "strict_stamp": s["row"].get("mom_version_stamp"),
        "loose_stamp": l["row"].get("mom_version_stamp"),
        "verdict": verdict,
    })
rows.sort(key=lambda r: -(abs(r["win_spread_loose_minus_strict"]) if r["win_spread_loose_minus_strict"] is not None else 99))
out = os.path.join(DESK, "eval_comparison.csv")
with open(out, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader(); w.writerows(rows)
print(f"saved {out}\n")


def fmt(v):
    return f"{v:5.1f}" if isinstance(v, float) else "  -- "


print(f"{'account':32s} {'win b/s/l':>19s} {'mom b/s/l':>19s} {'wspread':>7s}  verdict")
for r in rows:
    ws = r["win_spread_loose_minus_strict"]
    print(f"{r['account'][:32]:32s} {fmt(r['win_base'])}/{fmt(r['win_strict'])}/{fmt(r['win_loose'])} "
          f"{fmt(r['mom_base'])}/{fmt(r['mom_strict'])}/{fmt(r['mom_loose'])} "
          f"{(f'{ws:7.1f}' if ws is not None else '     --')}  {r['verdict']}")
