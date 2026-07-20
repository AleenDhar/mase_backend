"""Local QA: scan deal records for drawer-quality issues. python qa_drawer.py <json> <label> ..."""
import json, sys, re
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PILLARS = ("metrics", "economic_buyer", "decision_criteria", "decision_process",
           "paper_process", "identify_pain", "champion", "competition")
HORIZONS = ("next_7_days", "next_14_days", "next_30_days")


def _titled(text):
    """A bullet reads ACEN-style if it leads with a short headline then ' — ' / ': '."""
    head = (text or "")[:70]
    return (" — " in head) or (" - " in head) or bool(re.match(r"^[A-Z][^.]{3,45}: ", head))


def check(rec, label):
    iss = []
    ai = rec.get("ai") or {}
    hard = rec.get("hard") or {}
    ds = ai.get("deal_scores") or {}
    hl = ds.get("headline") or {}

    for k in ("win_position", "deal_momentum", "customer_commitment", "deal_risk", "forecast_confidence"):
        if hl.get(k) is None:
            iss.append(f"score {k} is None")
    if ds.get("factor_source") != "ai":
        iss.append(f"factor_source={ds.get('factor_source')} (expected ai)")
    if ds.get("scoring_degraded"):
        iss.append(f"scoring_degraded set: {ds.get('fallback_reason')}")

    reasons = ds.get("ai_reasons") or {}
    for key in ("win_position", "deal_momentum"):
        rs = reasons.get(key) or []
        if len(rs) < 4:
            iss.append(f"{key}: only {len(rs)} reasons")
        short = [b.get("text", "")[:45] for b in rs if len(b.get("text") or "") < 60]
        if short:
            iss.append(f"{key}: {len(short)} thin reason(s) e.g. {short[0]!r}")
        titled = sum(1 for b in rs if _titled(b.get("text")))
        if rs and titled < len(rs):
            iss.append(f"{key}: only {titled}/{len(rs)} reasons have an ACEN-style headline")

    cp = ds.get("cro_panel") or {}
    for b in (cp.get("blocks") or []):
        if b.get("kind") != "score":
            continue
        notitle = [it for it in (b.get("items") or [])
                   if isinstance(it, dict) and not it.get("title") and not _titled(it.get("text"))]
        if notitle:
            iss.append(f"cro '{b.get('title')}': {len(notitle)}/{len(b.get('items') or [])} bullets have NO title")

    md = ai.get("meddpicc") or {}
    miss = [p for p in PILLARS if not (isinstance(md.get(p), dict) and (md[p].get("narrative") or md[p].get("value")))]
    if miss:
        iss.append(f"meddpicc missing narrative: {miss}")

    stk = (ai.get("stakeholder_map") or {}).get("items") or []
    empt = [s.get("name") for s in stk if not s.get("last_contact_date") and not (s.get("sentiment") and s.get("sentiment") != "None")]
    if len(empt) >= 2:
        iss.append(f"{len(empt)} stakeholders with no engagement data (name/title only): {empt[:5]}")

    comps = (ai.get("competitive_position") or {}).get("competitors") or []
    norm = [re.sub(r"[^a-z0-9]", "", str(c.get("name") or "").lower()) for c in comps]
    for token in ("zip", "netsuite", "coupa", "ariba", "ivalua"):
        n = sum(1 for x in norm if token in x)
        if n > 1:
            iss.append(f"competitor DUPLICATE: '{token}' appears in {n} rows -> {[c.get('name') for c,x in zip(comps,norm) if token in x]}")

    for bucket in ("explicit_requirements",):
        items = (ai.get(bucket) or {}).get("items") or []
        texts = [str(x.get("requirement") or x.get("deliverable") or "").strip().lower() for x in items]
        d = len(texts) - len(set(texts))
        if d:
            iss.append(f"{bucket}: {d} duplicate item(s) of {len(texts)}")
    ir = ai.get("implicit_requirements") or {}
    for sub in ("we_promised", "buyer_dependent"):
        items = (ir.get(sub) or {}).get("items") or []
        texts = [str(x.get("deliverable") or "").strip().lower() for x in items]
        d = len(texts) - len(set(texts))
        if d:
            iss.append(f"implicit.{sub}: {d} duplicate item(s) of {len(texts)}")

    if not (ai.get("day_summary") or {}).get("overall"):
        iss.append("day_summary.overall empty")

    rm = (ai.get("recommended_moves") or {}).get("items") or []
    if len(rm) < 3:
        iss.append(f"only {len(rm)} recommended_moves")
    hz = set(m.get("horizon") for m in rm)
    for h in HORIZONS:
        if h not in hz:
            iss.append(f"no recommended_move for {h}")
    undated = [m.get("action", "")[:30] for m in rm if not m.get("act_by")]
    if undated:
        iss.append(f"{len(undated)} move(s) with no act_by date")

    vul = ai.get("vulnerabilities")
    vitems = vul.get("items") if isinstance(vul, dict) else (vul if isinstance(vul, list) else None)
    if not vitems:
        iss.append("vulnerabilities EMPTY")

    ceo = ai.get("ceo_intervention") or {}
    if ceo.get("needed") and not ceo.get("summary"):
        iss.append("ceo_intervention.needed but summary BLANK")
    for r in (ceo.get("reasons") or []):
        ao = str(r.get("as_of") or "")
        if ao and ao < "2026-04-10":   # >90d before 2026-07-09
            iss.append(f"ceo watch OLDER than 90d survived: {r.get('type')} as_of={ao}")

    print(f"\n===== {label}  ({rec.get('opp_id') or hard.get('opp_id')})  —  {len(iss)} issue(s) =====")
    for i in iss:
        print("  -", i)
    return iss


args = sys.argv[1:]
for i in range(0, len(args), 2):
    try:
        rec = json.load(open(args[i], encoding="utf-8"))
    except Exception as e:
        print(f"\n{args[i+1]}: cannot read ({e})")
        continue
    check(rec, args[i + 1])
