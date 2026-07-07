"""DIAGNOSTIC (read-only, no writes): how much is the momentum->win coupling inflating win
position across the book? Reads the stored win_position breakdown on every active deal,
reconstructs the pre-ceiling raw win, and computes the counterfactual win WITHOUT the momentum
lift. Answers: how many deals are pegged at their stage ceiling, and how many are pegged only
BECAUSE of the momentum lift (i.e. would fall below the cap if momentum weren't pulling them up)."""
import sys
import requests, urllib3
from daily_summary.common import load_secret, VERIFY
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sec = load_secret(); SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}


def contrib_pts(wp, key):
    for c in (wp.get("contributions") or []):
        if c.get("key") == key:
            return float(c.get("points") or 0)
    return 0.0


def raw_and_counterfactual(wp):
    """Reconstruct pre-ceiling raw win and the win WITHOUT the momentum coupling."""
    anchor = float(wp.get("anchor") or 0); lift = float(wp.get("lift") or 0)
    madj = float(wp.get("momentum_adj") or 0); scope = float(wp.get("scope_adj") or 0)
    trend = float(wp.get("trend_nudge") or 0); risk = float(wp.get("risk_penalty") or 0)
    fc = contrib_pts(wp, "forecast_conviction"); rel = contrib_pts(wp, "relationship_leverage")
    raw = anchor + lift + madj + scope + trend + risk + fc + rel
    ceil = float(wp.get("ceiling") or 70)
    clamp = lambda x: min(ceil, max(5.0, min(99.0, max(0.0, x))))
    return raw, clamp(raw), clamp(raw - madj), ceil, madj


rows = requests.get(f"{SB}/rest/v1/deal_records", params={"select": "account_name,stage,record", "active": "eq.true", "limit": "700"},
                    headers=H, verify=VERIFY, timeout=180).json()

tot = pegged = mom_pushed = big_madj = 0
madj_vals = []
biggest = []          # (madj, name, stage, score, score_no_mom)
pinsent = None
for r in rows:
    ai = (r.get("record") or {}).get("ai") or {}
    ds = ai.get("deal_scores") or {}; wp = ds.get("win_position") or {}
    hl = ds.get("headline") or {}
    w = hl.get("win_position")
    if not isinstance(w, (int, float)) or not wp:
        continue
    pinned = bool(ds.get("pinned") or ai.get("pinned"))
    raw, score, score_no_mom, ceil, madj = raw_and_counterfactual(wp)
    tot += 1; madj_vals.append(madj)
    is_pegged = raw >= ceil - 0.05
    if is_pegged:
        pegged += 1
    # momentum is what pushed it to the cap: pegged now, but below cap without the momentum lift
    if is_pegged and score_no_mom < ceil - 1.0 and not pinned:
        mom_pushed += 1
    if madj >= 15:
        big_madj += 1
    biggest.append((round(madj, 1), str(r.get("account_name"))[:26], str(r.get("stage"))[:16], w, round(score_no_mom, 1), pinned, raw, ceil))
    if "pinsent" in str(r.get("account_name") or "").lower():
        pinsent = (r.get("account_name"), r.get("stage"), wp, raw, score, score_no_mom, ceil, madj, hl.get("deal_momentum"))

print(f"=== MOMENTUM -> WIN INFLATION (book-wide, {tot} scored live deals) ===")
print(f"pegged AT their stage ceiling:            {pegged}  ({100*pegged//max(tot,1)}%)")
print(f"  of which pegged ONLY due to momentum:   {mom_pushed}  (would fall below the cap without the momentum lift)")
print(f"momentum_adj >= +15 pts:                  {big_madj}  ({100*big_madj//max(tot,1)}%)")
print(f"momentum_adj mean:                        {sum(madj_vals)/max(len(madj_vals),1):+.1f}   max {max(madj_vals):+.1f}   min {min(madj_vals):+.1f}")

# Model the impact of CAPPING the above-expected momentum lift (down-drag untouched).
print("\n--- IMPACT of capping the UP-side momentum lift (downside drag unchanged) ---")
clampd = lambda x, c: min(c, max(5.0, min(99.0, max(0.0, x))))
for cap in (10.0, 12.0, 15.0):
    moved = 0; drops = []
    for madj, nm, st, w, wnm, pinned, raw, ceil in biggest:
        if pinned or madj <= cap:
            continue
        new = clampd(raw - madj + cap, ceil)   # apply capped lift to the true raw, then re-clamp
        d = round(w - new, 1)
        if d >= 0.1:
            moved += 1; drops.append((d, nm, w, round(new, 1)))
    drops.sort(reverse=True)
    avg = sum(d for d, *_ in drops) / max(len(drops), 1)
    print(f"  cap +{int(cap):<2}: {moved:>3} deals drop  | avg drop {avg:.1f}  | biggest: " +
          ", ".join(f"{nm.split(',')[0][:16]} {w}->{n}" for d, nm, w, n in drops[:4]))

print("\n--- Top 15 by momentum lift (madj) — 'win_no_mom' = win if momentum coupling removed ---")
for madj, nm, st, w, wnm, pinned in sorted(biggest, reverse=True)[:15]:
    tag = " [PIN]" if pinned else ""
    print(f"  madj {madj:+5}  {nm:26} {st:16} win={w:>4}  win_no_mom={wnm:>4}{tag}")

if pinsent:
    nm, st, wp, raw, score, snm, ceil, madj, mom = pinsent
    print(f"\n=== PINSENT MASONS breakdown ===")
    print(f"  stage={st} ceiling={ceil} momentum={mom}")
    print(f"  anchor={wp.get('anchor')} lift={wp.get('lift')} momentum_adj={wp.get('momentum_adj')} "
          f"trend={wp.get('trend_nudge')} scope={wp.get('scope_adj')} risk={wp.get('risk_penalty')}")
    print(f"  RAW (pre-ceiling)={raw:.1f}  ->  clamped win={score:.1f}   |   win WITHOUT momentum lift={snm:.1f}")
