"""DRY-RUN sweep (datalake-test -> ab_test_results; NOTHING to deal_records) for a named opp
list on the DEPLOYED pipeline, then print EVERY deal-drawer surface for the user to judge.

  python run_drawer.py            # Allstate + Consumer Cellular
"""
import sys, json, textwrap
import dryrun_fleet as D

OPPS = [
    {"opp_id": "006P7000006uKrq", "opp_name": "Allstate S2P",         "account_name": "Allstate"},
    {"opp_id": "0066700000wdNe1", "opp_name": "Cebu Pacific Air S2P",  "account_name": "Cebu Pacific Air"},
]
if len(sys.argv) > 1 and sys.argv[1] == "collect":
    OPPS = OPPS  # just re-collect + print from ab_test_results


def w(label, val, width=100):
    s = "" if val is None else str(val)
    print(f"{label}: " + ("\n".join(textwrap.wrap(s, width)) if len(s) > width else s))


def bullets(items, fmt):
    for it in (items or []):
        try:
            print("   - " + fmt(it))
        except Exception as e:
            print(f"   - <unprintable: {e}>")


def print_drawer(rec):
    if not rec:
        print("  (no record)")
        return
    hard = rec.get("hard") or {}
    ai = rec.get("ai") or {}
    ds = ai.get("deal_scores") or {}
    hl = ds.get("headline") or {}
    ev = rec.get("evidence_coverage") or {}

    print("\n" + "#" * 90)
    print(f"# {rec.get('account_name') or hard.get('account')}  —  {rec.get('opp_name')}")
    print("#" * 90)

    print("\n== HEADER (hard facts) ==")
    w("  stage", hard.get("stage")); w("  amount", hard.get("amount"))
    w("  close_date", hard.get("close_date")); w("  forecast_category", hard.get("forecast_category"))
    w("  owner", hard.get("owner_name")); w("  next_step", hard.get("next_step"))
    w("  last_activity_date", hard.get("last_activity_date"))

    print("\n== EVIDENCE COVERAGE ==")
    w("  calls_discovered", ev.get("calls_discovered")); w("  calls_read", ev.get("calls_read"))
    w("  discovery_method", ev.get("discovery_method")); w("  gaps", ev.get("gaps"))
    w("  confidence", ev.get("confidence") or ai.get("analysis_confidence"))

    print("\n== DEAL SCORES ==")
    print(f"  WIN {hl.get('win_position')} | MOMENTUM {hl.get('deal_momentum')} | read={hl.get('read')} "
          f"| commit={hl.get('customer_commitment')} risk={hl.get('deal_risk')} "
          f"forecast_conf={hl.get('forecast_confidence')}")
    w("  factor_source", ds.get("factor_source")); w("  scoring_degraded", ds.get("scoring_degraded"))
    reasons = ds.get("ai_reasons") or {}
    for key in ("win_position", "deal_momentum", "customer_commitment", "deal_risk"):
        rs = reasons.get(key) or []
        if rs:
            print(f"  -- {key} reasons ({len(rs)}) --")
            bullets(rs, lambda b: f"[{b.get('tone')}] {b.get('text')}")

    print("\n== 24-HOUR SUMMARY (day_summary) ==")
    d24 = ai.get("day_summary") or {}
    w("  overall", d24.get("overall"))
    bullets(d24.get("items"), lambda it: f"{it.get('at')} [{it.get('kind')}] {it.get('name')}: {it.get('summary')}")

    print("\n== FORECAST READ ==")
    fr = ai.get("forecast_read") or {}
    w("  defensible", fr.get("defensible")); w("  recommended_forecast", fr.get("recommended_forecast"))
    w("  reason", fr.get("reason"))

    print("\n== MEDDPICC ==")
    md = ai.get("meddpicc") or {}
    if isinstance(md, dict):
        for k, v in md.items():
            if isinstance(v, dict):
                w(f"  {k}", v.get("value") or v.get("text") or v.get("summary") or json.dumps(v)[:200])
            else:
                w(f"  {k}", v)

    print("\n== STAKEHOLDER MAP ==")
    stk = (ai.get("stakeholder_map") or {}).get("items") if isinstance(ai.get("stakeholder_map"), dict) else ai.get("stakeholders")
    bullets(stk, lambda s: f"{s.get('name')} ({s.get('title')}) — {s.get('role')} | sentiment={s.get('sentiment')} risk={s.get('risk')} last={s.get('last_contact_date')}")

    print("\n== COMPETITIVE POSITION ==")
    cp = ai.get("competitive_position") or {}
    w("  summary", cp.get("summary"))
    bullets(cp.get("competitors"), lambda c: f"{c.get('name')} — {c.get('sentiment')}/{c.get('threat_level')} ({c.get('status')}) | how_we_win: {c.get('how_we_win')}")

    print("\n== CRITICAL SIGNALS ==")
    bullets(ai.get("critical_signals"), lambda s: f"[{s.get('tone')}] {s.get('lens')}: {s.get('text')}")

    print("\n== RECOMMENDED MOVES (to-dos) ==")
    rm = (ai.get("recommended_moves") or {}).get("items") if isinstance(ai.get("recommended_moves"), dict) else ai.get("recommended_moves")
    bullets(rm, lambda m: f"[rank {m.get('rank')}] ({m.get('horizon')}) {m.get('action')} — act_by {m.get('act_by')} | {m.get('expected_effect')}")

    print("\n== REQUIREMENTS ==")
    er = ai.get("explicit_requirements") or {}
    ir = ai.get("implicit_requirements") or {}
    wep = (ir.get("we_promised") or {}).get("items") if isinstance(ir.get("we_promised"), dict) else None
    bdp = (ir.get("buyer_dependent") or {}).get("items") if isinstance(ir.get("buyer_dependent"), dict) else None
    if isinstance(er, dict) and er.get("items"):
        print("  explicit:"); bullets(er["items"], lambda x: f"{x.get('requirement') or x.get('deliverable')} ({x.get('status')})")
    if wep:
        print("  we_promised:"); bullets(wep, lambda x: f"{x.get('deliverable')} — due {x.get('due')} ({x.get('status')})")
    if bdp:
        print("  buyer_dependent:"); bullets(bdp, lambda x: f"{x.get('deliverable')} — due {x.get('due')} ({x.get('status')})")

    print("\n== VULNERABILITIES ==")
    vul = ai.get("vulnerabilities")
    if isinstance(vul, list):
        bullets(vul, lambda v: v if isinstance(v, str) else json.dumps(v)[:200])
    elif vul:
        w("  ", vul)

    print("\n== CEO INTERVENTION ==")
    ceo = ai.get("ceo_intervention") or {}
    w("  needed", ceo.get("needed")); w("  summary", ceo.get("summary") or ceo.get("rationale"))


if __name__ == "__main__" and (len(sys.argv) < 2 or sys.argv[1] != "collect"):
    try:
        h = D.requests.get(f"{D.API}/api/health", headers=D.AH, verify=False, timeout=30)
        print(f"API {D.API} health -> {h.status_code}", flush=True)
    except Exception as e:
        print(f"API health FAILED: {e}", flush=True); sys.exit(1)
    print(f"dry-run sweep: {[o['account_name'] for o in OPPS]}", flush=True)
    D.run_batch(list(OPPS), validate_mode=False)

# collect + print full drawer for each
for o in OPPS:
    st = D.poll(o["opp_id"]) or {}
    rec = (st.get("result") or {}).get("record") if isinstance(st.get("result"), dict) else None
    if rec:
        with open(f"dryrun_forecasted/{o['opp_id']}.json", "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1, default=str)
    print_drawer(rec)
