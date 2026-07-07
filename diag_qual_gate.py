"""READ-ONLY prototype+measurement (no writes) of the QUALIFICATION-GATED win position.

User's 7-point-drill logic: a HIGH win probability must be EARNED by ticking qualification boxes
first. Access to Power (economic buyer) is the dominant gate -- you can't be confident of winning a
deal you have no path to sign. So Win is CEILINGED by qualification, applied after the raw compute,
so momentum/preference can't lift a deal past what its qualification supports.

Refinement (stage-authority): once the HARD SF stage is Vendor Selected+ the stage ITSELF proves
access to power (you don't get selected without the EB), so the qualification cap lifts -- legit
late-stage deals (Publicis/Swift/Mair) are untouched. The gate only bites on deals still evaluating
(Qualified / Formal Eval / Shortlisted) that read a confident win with empty qualification boxes."""
import sys, copy
import requests, urllib3
import deal_engine_scoring as SC
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sec = load_secret(); SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
_ORIG_OVERRIDE = SC._selection_override
_ORIG_CEILING = SC._win_ceiling

EB_CEIL = {"confirmed": 100.0, "partial": 74.0, "gap": 52.0}
COMP_CEIL = {"confirmed": 100.0, "partial": 90.0, "gap": 66.0}
CHAMP_CEIL = {"confirmed": 100.0, "partial": 86.0, "gap": 60.0}
MISS = {"eb": 50.0, "comp": 66.0, "champ": 58.0}
_POST = ("vendor select", "selected", "negotiat", "contract", "won", "po received", "po-", "closed")


def _mst(ai, k):
    md = ai.get("meddpicc") or {}
    v = md.get(k) if isinstance(md, dict) else None
    return str((v.get("status") if isinstance(v, dict) else v) or "").strip().lower()


def qual_ceiling(record):
    hard = (record or {}).get("hard") or {}; ai = (record or {}).get("ai") or {}
    stage = str(hard.get("stage") or "").lower()
    if any(t in stage for t in _POST):
        return 100.0, "", ""
    eb, comp, ch = _mst(ai, "economic_buyer"), _mst(ai, "competition"), _mst(ai, "champion")
    exp = ai.get("expansion_context")
    if isinstance(exp, dict) and exp.get("prior_closed_won") and eb == "gap":
        eb = "partial"    # expansion into a won account = we already hold access
    caps = [(EB_CEIL.get(eb, MISS["eb"]), "economic buyer", eb or "missing"),
            (COMP_CEIL.get(comp, MISS["comp"]), "competitive field", comp or "missing"),
            (CHAMP_CEIL.get(ch, MISS["champ"]), "champion", ch or "missing")]
    return min(caps, key=lambda x: x[0])


def patched_ceiling(record):
    base = _ORIG_CEILING(record)
    cap, _, _ = qual_ceiling(record)
    return min(base, cap)


def hardened_override(record, strengths=None):
    ai = (record or {}).get("ai") or {}; hard = (record or {}).get("hard") or {}
    stg = str(hard.get("stage") or "").strip().lower()
    if not stg or any(t in stg for t in ("initial interest", "qualified", "prospect", "discovery", "lead", "1.", "2.")):
        return False
    if _mst(ai, "economic_buyer") != "confirmed":      # a selection is made by an economic buyer
        return False
    nv = ai.get("north_star_verdict") or {}
    if str(nv.get("verdict") or "").lower() in ("slowing", "stalled", "at risk", "cooling"):
        return False
    st = strengths if isinstance(strengths, dict) else SC._rubric_win_strengths(record or {})
    cp = ai.get("customer_preference") or {}; _ps = st.get("preference")
    if not (str(cp.get("level") or cp.get("status") or "").lower() == "high"
            or (isinstance(_ps, (int, float)) and float(_ps) >= 0.9)):
        return False
    if SC._competitive_strength(ai) <= 0:
        return False
    return True


def win_of(rec):
    return (SC.compute_deal_scores(rec).get("headline") or {}).get("win_position")


rows = requests.get(f"{SB}/rest/v1/deal_records", params={"select": "account_name,stage,record", "active": "eq.true", "limit": "700"},
                    headers=H, verify=VERIFY, timeout=180).json()
live = [r for r in rows if isinstance((((r.get("record") or {}).get("ai") or {}).get("deal_scores", {}).get("headline", {}) or {}).get("win_position"), (int, float))]

SC._selection_override = hardened_override
SC._win_ceiling = patched_ceiling
watch = ("barnes", "sgd pharma", "pinsent", "publicis", "swift sc", "mair group", "omnia", "avaya", "russell investments")
moved = []; spot = {}
for r in live:
    rec = copy.deepcopy(r["record"]); nm = str(r.get("account_name") or "")
    pinned = bool((rec.get("ai") or {}).get("deal_scores", {}).get("pinned") or (rec.get("ai") or {}).get("pinned"))
    old = (rec.get("ai") or {}).get("deal_scores", {}).get("headline", {}).get("win_position")
    new = win_of(rec)
    if isinstance(old, (int, float)) and isinstance(new, (int, float)) and abs(old - new) >= 0.1 and not pinned:
        moved.append((round(old - new, 1), nm[:26], str(r.get("stage"))[:16], old, new))
    for w in watch:
        if w in nm.lower():
            cap, lab, stt = qual_ceiling(rec)
            spot[nm[:26]] = (old, new, str(r.get("stage"))[:16], round(cap, 0), lab, stt, pinned)
SC._selection_override = _ORIG_OVERRIDE; SC._win_ceiling = _ORIG_CEILING

drops = [m for m in moved if m[0] > 0]
print(f"=== QUALIFICATION-GATED WIN — book impact ({len(live)} live deals) ===")
print(f"deals changed: {len(moved)}  | drops {len(drops)}  | avg drop {sum(m[0] for m in drops)/max(len(drops),1):.1f}  | max drop {max((m[0] for m in drops), default=0)}")
print("\n-- spot check (watch list): old -> new  [stage | qual-cap by <box>=<status>] --")
for nm, (o, n, st, cap, lab, stt, pin) in sorted(spot.items(), key=lambda x: -(x[1][0] or 0)):
    tag = " PINNED" if pin else ""
    print(f"  {nm:26} {str(o):>5} -> {str(n):<5} [{st:16} cap {cap:>4.0f} by {lab}={stt}]{tag}")
print("\n-- 15 biggest drops --")
for d, nm, st, o, n in sorted(drops, reverse=True)[:15]:
    print(f"  -{d:<5} {nm:26} {st:16} {o} -> {n}")
