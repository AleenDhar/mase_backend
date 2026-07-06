"""CEO-attention APPLY — read the judge verdicts and REPLACE ai.ceo_intervention
with the unified {support, monitor} shape for every win>=40 opp. Deterministic
guards: win>=40 eligibility re-checked; monitor triggers stripped if they lack a
<=14-day as_of; buyer_target names verified against the pack (SFDC), else role-only.
Opp-scoped jsonb_set via the Supabase Management API. Local, $0."""
from __future__ import annotations
import os, re, json, glob, datetime as dt
import requests, urllib3
from daily_summary.common import load_secret, VERIFY, id15

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
LARGE = 250000.0
WINDOW_DAYS = 14
LEVERS = {"pricing", "product", "presales_resources", "exec_connect"}
TRIGGERS = {"our_slip", "large_slowdown", "competitor_edge"}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ceo_attention")


def _recent(as_of, today):
    try:
        d = dt.date.fromisoformat(str(as_of)[:10])
    except Exception:
        return False
    return 0 <= (today - d).days <= WINDOW_DAYS + 1  # small grace


def build_attention(v, today, gen):
    """UNIFIED CEO attention: ONE flag + ONE list of reasons. 'support' (the CEO must
    ACT — pricing/product/presales_resources/exec_connect) is just one reason TYPE
    inside the watchlist, automatically included alongside the watch triggers
    (our_slip / large_slowdown / competitor_edge). No separate support/monitor split."""
    reasons = []
    # (a) support -> an ACT reason (the CEO uses his veto/availability)
    s = v.get("support") if isinstance(v.get("support"), dict) else {}
    if s.get("needed"):
        areas = [a for a in (s.get("areas") or []) if a in LEVERS] or ["exec_connect"]
        reasons.append({"type": "support", "act": True,
                        "severity": s.get("priority") if s.get("priority") in ("high", "medium") else "high",
                        "areas": areas, "summary": s.get("summary") or s.get("reason"),
                        "detail": s.get("detail"), "metric": s.get("metric"), "owner": s.get("owner"),
                        "ceo_action": s.get("ceo_action"), "ceo_ask": s.get("ceo_ask"),
                        "buyer_target": s.get("buyer_target") or {},
                        "why_not_vp": s.get("why_not_vp"), "as_of": gen})
    # (b) monitor triggers -> WATCH reasons, each HARD-gated to a <=14-day as_of
    m = v.get("monitor") if isinstance(v.get("monitor"), dict) else {}
    for t in (m.get("triggers") or []):
        if not isinstance(t, dict) or t.get("type") not in TRIGGERS:
            continue
        if not _recent(t.get("as_of"), today):   # drop stale evidence
            continue
        reasons.append({"type": t.get("type"), "act": False,
                        "severity": t.get("severity") if t.get("severity") in ("high", "medium") else "medium",
                        "summary": t.get("summary"), "detail": t.get("detail"),
                        "metric": t.get("metric"), "owner": t.get("owner"),
                        "ceo_ask": t.get("ceo_ask"), "evidence": t.get("evidence"),
                        "as_of": str(t.get("as_of"))[:10]})
    needed = bool(reasons)
    severity = "high" if any(r.get("severity") == "high" for r in reasons) else "medium"
    needs_action = any(r.get("type") == "support" for r in reasons)
    return needed, severity, needs_action, reasons


def main():
    sec = load_secret()
    base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
    mgmt = f"https://api.supabase.com/v1/projects/{ref}/database/query"
    token = sec["SUPABASE_ACCESS_TOKEN"]
    today = dt.date.today()
    gen = today.isoformat()

    # re-derive win>=40 eligibility + win/mom from prod (source of truth)
    rows = requests.get(f"{base}/rest/v1/deal_records",
                        params={"select": "opp_id,record", "active": "eq.true"},
                        headers={"apikey": key, "Authorization": f"Bearer {key}"}, verify=VERIFY, timeout=120).json()
    winmom = {}
    existing_scope = {}   # native scope_shrink watches to PRESERVE (computed by the sweep)
    for r in rows:
        oid = id15(r["opp_id"])
        ai = (r.get("record") or {}).get("ai") or {}
        hl = ai.get("deal_scores", {}).get("headline", {}) or {}
        ci = ai.get("ceo_intervention") or {}
        ss = [x for x in (ci.get("reasons") or []) if isinstance(x, dict) and x.get("type") == "scope_shrink"]
        if ss:
            existing_scope[oid] = ss
        try:
            w = float(hl.get("win_position"))
        except (TypeError, ValueError):
            continue
        if w >= 40:
            winmom[oid] = (w, hl.get("deal_momentum"))

    verdicts = {}
    for fp in glob.glob(os.path.join(OUT, "*.verdict.json")):
        try:
            v = json.load(open(fp, encoding="utf-8"))
            verdicts[id15(v.get("opp_id"))] = v
        except Exception as e:
            print("bad verdict", os.path.basename(fp), e)

    # Only WRITE opps we actually judged this run (have a verdict). Deals without a verdict
    # are left UNTOUCHED — critical so this run doesn't clobber the forecasted deals' fresh
    # sweep CEO (support + scope_shrink) with a bare needed:false.
    out = {}
    n_attn = n_act = 0
    for opp, (w, m) in winmom.items():
        if opp not in verdicts:
            continue
        v = verdicts.get(opp) or {}
        needed, severity, needs_action, reasons = build_attention(v, today, gen)
        # PRESERVE any native scope_shrink watch (the judge doesn't produce it).
        reasons = existing_scope.get(opp, []) + reasons
        needed = bool(reasons)
        severity = "high" if any(rr.get("severity") == "high" for rr in reasons) else "medium"
        needs_action = any(rr.get("type") == "support" for rr in reasons)
        out[opp] = {"needed": needed, "severity": severity if needed else None,
                    "needs_action": needs_action, "reasons": reasons,
                    "win": w, "mom": m, "source": "attention_v1", "generated_at": gen}
        n_attn += needed; n_act += needs_action

    print(f"win>=40: {len(winmom)} | verdicts: {len(verdicts)} | CEO attention {n_attn} "
          f"(of which need CEO to ACT: {n_act})")
    if "--apply" not in os.sys.argv:
        print("[DRY RUN] pass --apply to write. sample:")
        for opp in [o for o in out if out[o]["needed"]][:2]:
            print(" ", opp, json.dumps(out[opp])[:260])
        return

    blob = json.dumps(out)
    sql = ("update deal_records d set record = jsonb_set(record,'{ai,ceo_intervention}', m.value, true), "
           "updated_at = now() from (select key as opp_id, value from jsonb_each($J$" + blob + "$J$::jsonb)) m "
           "where d.opp_id = m.opp_id returning d.opp_id")
    r = requests.post(mgmt, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                      json={"query": sql}, verify=VERIFY, timeout=120)
    if r.status_code >= 300:
        print("APPLY FAILED", r.status_code, r.text[:300]); return
    print(f"APPLIED (source=attention_v1): {len(r.json())} rows | CEO attention {n_attn} | need to act {n_act}")


if __name__ == "__main__":
    main()
