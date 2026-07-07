"""READ-ONLY prototype+measurement (no writes). Models two playbook-grounded fixes and shows the
book-wide impact, so we decide before shipping:

  A) HARDEN the selection override: it may fire ONLY when the HARD CRM corroborates a selection --
     forecast_category in {Commit, Best Case}, an actual won/commit signal (not mere
     'forecast_defensible', which defends even a Pipeline), the engine's own verdict is not
     Slowing/weaker, and there's a POSITIVE competitive edge (not just 'unknown rivals').

  B) COUPLE the win ceiling to the rep's forecast conviction: the engine cannot read a deal as a
     stronger win than the CRM's own forecast category supports (stage-authority: hard facts win).
     Commit->stage ceiling, Best Case->90, Upside->80, Pipeline->62, Omitted/blank->55.

Prints B&N + the override deals + the book delta under A, and under A+B."""
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
CONV = {"commit": 100.0, "best case": 90.0, "upside key deal": 80.0, "upside": 80.0,
        "pipeline": 62.0, "omitted": 55.0, "omitted from forecast": 55.0, "": 55.0}


def hardened_override(record, strengths=None):
    ai = (record or {}).get("ai") or {}; hard = (record or {}).get("hard") or {}
    stg = str(hard.get("stage") or "").strip().lower()
    if not stg or any(t in stg for t in ("initial interest", "qualified", "prospect", "discovery", "lead", "1.", "2.")):
        return False
    fc = str(hard.get("forecast_category") or "").strip().lower()
    if fc not in ("commit", "best case"):
        return False                      # rep has no conviction -> engine must not claim a win
    nv = ai.get("north_star_verdict") or {}
    if str(nv.get("verdict") or "").lower() in ("slowing", "stalled", "at risk", "cooling") \
       or str(nv.get("trajectory") or "").lower() in ("weaker", "declining", "cooling"):
        return False                      # engine's own verdict says weak
    dec = str((ai.get("decision_outcome") or {}).get("status") or "").lower()
    rec_fc = str(nv.get("recommended_forecast") or "").lower()
    if not (dec in ("won", "selected") or rec_fc.startswith("commit") or rec_fc.startswith("best")):
        return False                      # need a real selection/commit signal, not 'defensible'
    st = strengths if isinstance(strengths, dict) else SC._rubric_win_strengths(record or {})
    cp = ai.get("customer_preference") or {}; _ps = st.get("preference")
    pref_high = (str(cp.get("level") or cp.get("status") or "").lower() == "high"
                 or (isinstance(_ps, (int, float)) and float(_ps) >= 0.9))
    if not pref_high:
        return False
    if SC._competitive_strength(ai) <= 0:
        return False                      # require a POSITIVE edge, not merely 'unknown rivals'
    return True


def conviction_ceiling(record):
    base = _ORIG_CEILING(record)
    fc = str(((record or {}).get("hard") or {}).get("forecast_category") or "").strip().lower()
    return min(base, CONV.get(fc, 62.0))


def win_of(rec):
    sc = SC.compute_deal_scores(rec)
    return (sc.get("headline") or {}).get("win_position")


rows = requests.get(f"{SB}/rest/v1/deal_records", params={"select": "account_name,stage,record", "active": "eq.true", "limit": "700"},
                    headers=H, verify=VERIFY, timeout=180).json()
live = [r for r in rows if isinstance((((r.get("record") or {}).get("ai") or {}).get("deal_scores", {}).get("headline", {}) or {}).get("win_position"), (int, float))]


def run(label, harden, ceil):
    SC._selection_override = hardened_override if harden else _ORIG_OVERRIDE
    SC._win_ceiling = conviction_ceiling if ceil else _ORIG_CEILING
    moved = []; bn = None
    for r in live:
        rec = copy.deepcopy(r["record"])
        pinned = bool((rec.get("ai") or {}).get("deal_scores", {}).get("pinned") or (rec.get("ai") or {}).get("pinned"))
        old = (rec.get("ai") or {}).get("deal_scores", {}).get("headline", {}).get("win_position")
        new = win_of(rec)
        if isinstance(old, (int, float)) and isinstance(new, (int, float)) and abs(old - new) >= 0.1 and not pinned:
            moved.append((round(old - new, 1), str(r.get("account_name"))[:24], str(r.get("stage"))[:16], old, new))
        if "barnes" in str(r.get("account_name") or "").lower():
            bn = (old, new, pinned)
    SC._selection_override = _ORIG_OVERRIDE; SC._win_ceiling = _ORIG_CEILING
    drops = [m for m in moved if m[0] > 0]
    print(f"\n=== {label} ===")
    if bn:
        print(f"  Barnes & Noble: win {bn[0]} -> {bn[1]}")
    print(f"  deals changed: {len(moved)}  (drops {len(drops)})  | avg drop {sum(m[0] for m in drops)/max(len(drops),1):.1f}")
    for d, nm, st, o, n in sorted(drops, reverse=True)[:12]:
        print(f"    -{d:<4} {nm:24} {st:16} {o} -> {n}")


run("SCENARIO A — hardened override only", True, False)
run("SCENARIO A+B — hardened override + conviction ceiling", True, True)
