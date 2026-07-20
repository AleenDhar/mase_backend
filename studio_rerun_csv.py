"""LOCAL-ONLY Scoring-Version-Studio rerun for named deals (Publicis Groupe, John Deere).

Consumes the studio-subagent outputs (cc_work/<opp>.studio.json + <opp>.engines.json),
applies the REAL production post-processing chain (validation + deterministic scorer with
the current local fixes) via cc_sweep.postprocess with NO upsert, simulates derive_todo's
four UI buckets on the NEW record, and writes ONE CSV with: scores old->new, deal-score
reasons, Signal Extraction / Deal-Reading, To-Do Generation (four sections), 24-Hour
Summary, and the why-only-best-practice-todos diagnosis.

WRITES NOTHING to Supabase / Salesforce. Reads nothing live (all inputs are local files
except the stored old scores already inside <opp>.ctx.json's prior record)."""
import sys, os, csv, json, re, copy, datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import cc_sweep
cc_sweep.load_env()

import deal_engine_scoring as SC
import deal_engine_store as store

WORK = cc_sweep.WORK
OUT = "studio_rerun_publicis_johndeere_2026-07-09.csv"
OPPS = [("006P700000Xl06R", "Publicis Groupe"), ("006P700000KHd9V", "John Deere")]

DIAG = {
    "006P700000Xl06R": ("UI folds recommended_moves under 'Best practices' by design; the 8-Jul sweep emitted "
                        "ZERO explicit_requirements / open_deliverables / implicit_requirements (both heads), so "
                        "Prospect requirements, Commitments made by Zycus and Waiting on the buyer were all empty "
                        "— even though the deal's own moves cite an on-call commitment ('Send the SoW as committed "
                        "on the 8 Jul call'), which belongs in we_promised with a grounding quote. Extraction gap."),
    "006P700000KHd9V": ("UI folds recommended_moves under 'Best practices' by design; the sweep DID capture 11 "
                        "open_deliverables + 3 explicit_requirements but every evidence date is 2025-06..2026-02 "
                        "(deal paused, buyer quiet since 11 May) — all older than the 90-day TODO_RECENCY_DAYS "
                        "gate, so they are filtered to context at read time. Fresh (May-Jul) signals were captured "
                        "only as moves/flags, never as dated asks/commitments. Recency gate + extraction gap."),
}


def reasons(block, cap=8):
    out = []
    for c in sorted((block or {}).get("contributions") or [], key=lambda x: -abs(float(x.get("points") or 0))):
        p = float(c.get("points") or 0)
        f = str(c.get("factor") or "?")
        ev = re.sub(r"\s+", " ", str(c.get("evidence") or "")).strip()[:130]
        if abs(p) < 0.05 and f not in ("qualification_gate", "verdict_reconcile", "risk_note", "false_velocity"):
            continue
        out.append(f"{f} {p:+.1f}: {ev}" if ev else f"{f} {p:+.1f}")
        if len(out) >= cap:
            break
    return " | ".join(out)


def joinlines(seq):
    """Join a list that may hold strings or dicts (some engines emit {text/reason/bullet:...})."""
    out = []
    for x in seq or []:
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            out.append(str(next((x[k] for k in ("text", "reason", "bullet", "point", "driver", "detail")
                                 if x.get(k)), json.dumps(x, default=str))))
        else:
            out.append(str(x))
    return "\n".join(out)


def fmt_items(items, *keys, cap=8):
    """Compact 'text (meta)' lines from a list of dicts."""
    out = []
    for it in (items or [])[:cap]:
        if isinstance(it, str):
            out.append(it)
            continue
        txt = next((it.get(k) for k in keys if it.get(k)), None) or json.dumps(it, default=str)[:90]
        meta = " ".join(f"{k}={it[k]}" for k in ("date", "due", "who", "who_asked", "committed_on", "needed_by")
                        if it.get(k))
        out.append(f"{txt}" + (f"  [{meta}]" if meta else ""))
    return "\n".join(out)


def simulate_ui_buckets(rec):
    """What the four UI todo buckets would hold for THIS record (derive_todo semantics,
    single record, no overrides/pushes — pure in-memory)."""
    ai = rec.get("ai") or {}
    today = dt.date.today()

    def fresh(dstr):
        d = store._parse_iso_date(dstr)
        return d is None or d >= today - dt.timedelta(days=store.TODO_RECENCY_DAYS)

    def horizon(dstr):
        d = store._parse_iso_date(dstr)
        return d is None or d <= today + dt.timedelta(days=store.TODO_HORIZON_DAYS)

    prospect, commitments, buyer_owed, best_pr = [], [], [], []
    # explicit -> prospect
    er = ai.get("explicit_requirements")
    for r in (er if isinstance(er, list) else (er or {}).get("items") or []):
        if not r.get("addressed") and fresh(r.get("date")):
            prospect.append(r.get("requirement"))
    # buyer_dependent -> waiting on buyer
    for d in store._buyer_dependent_items(ai):
        txt = d.get("deliverable") or d.get("commitment")
        st = (d.get("status") or "").lower()
        if txt and st in ("", "open", "overdue") and horizon(d.get("due")) and fresh(d.get("date")):
            buyer_owed.append(txt)
    # we_promised -> commitments (grounded) / best practices (ungrounded)
    for r in store._we_promised_items(ai):
        txt = r.get("deliverable") or r.get("inferred_need") or r.get("commitment")
        if not txt:
            continue
        if r.get("due"):
            st = (r.get("status") or "").lower()
            if st not in ("", "open", "overdue") or not horizon(r.get("due")) or not fresh(r.get("date")):
                continue
        elif not fresh(r.get("date")):
            continue
        if (r.get("grounding_quote") or "").strip() or (r.get("source") or "").strip():
            commitments.append(txt)
        else:
            best_pr.append(txt)
    # moves + flags -> best practices (UI displayBucketOf)
    mv = ai.get("recommended_moves")
    for m in (mv if isinstance(mv, list) else (mv or {}).get("items") or []):
        best_pr.append(m.get("action"))
    for f in ((ai.get("best_practice_check") or {}).get("flags") or [])[:store.TODO_MAX_BEST_PRACTICE]:
        best_pr.append(f if isinstance(f, str) else f.get("flag"))
    return prospect, commitments, buyer_owed, best_pr


rows_out = []
for oid, name in OPPS:
    sj = os.path.join(WORK, f"{oid}.studio.json")
    ej = os.path.join(WORK, f"{oid}.engines.json")
    cj = os.path.join(WORK, f"{oid}.ctx.json")
    if not (os.path.exists(sj) and os.path.exists(cj)):
        print(f"{name}: missing {os.path.basename(sj) if not os.path.exists(sj) else os.path.basename(cj)} — skip")
        continue
    raw = open(sj, encoding="utf-8").read()
    from opportunity_analyzer import _extract_json
    parsed = _extract_json(raw) if not raw.strip().startswith("{") else json.loads(raw)
    ctx = json.load(open(cj, encoding="utf-8"))
    engines = json.load(open(ej, encoding="utf-8")) if os.path.exists(ej) else {}

    old_ai = (ctx.get("existing") or {}).get("ai") or {}
    old_hl = (old_ai.get("deal_scores") or {}).get("headline") or {}

    rec, viol = cc_sweep.postprocess(copy.deepcopy(parsed), ctx["opp"], ctx["buyer"], ctx["existing"])
    json.dump(rec, open(os.path.join(WORK, f"{oid}.studio.final.json"), "w", encoding="utf-8"),
              indent=2, default=str)

    ai = rec.get("ai") or {}
    hl = (ai.get("deal_scores") or {}).get("headline") or {}
    wp = (ai.get("deal_scores") or {}).get("win_position") or {}
    mm = (ai.get("deal_scores") or {}).get("deal_momentum") or {}
    ev = ai.get("deal_scores_evidence") or {}
    air = ev.get("ai_reasons") or {}
    nsv = ai.get("north_star_verdict") or {}
    qc, qbox, qst = SC._qualification_ceiling(rec)

    prospect, commitments, buyer_owed, best_pr = simulate_ui_buckets(rec)
    tdo = engines.get("todo") or {}
    smm = engines.get("sum") or {}
    ext = engines.get("extract") or {}
    ewin = engines.get("win") or {}
    emom = engines.get("mom") or {}

    opp = ctx["opp"]
    rows_out.append({
        "account": name,
        "opp": opp.get("name"),
        "stage": opp.get("stage"),
        "forecast": opp.get("forecast_category"),
        "amount": opp.get("amount"),
        "close": opp.get("close_date"),
        "verdict_new": nsv.get("verdict"),
        "win_old_stored": old_hl.get("win_position"),
        "win_new_deterministic": hl.get("win_position"),
        "mom_old_stored": old_hl.get("deal_momentum"),
        "mom_new_deterministic": hl.get("deal_momentum"),
        "fc_conf_new": hl.get("forecast_confidence"),
        "risk_new": hl.get("deal_risk"),
        "commit_new": hl.get("customer_commitment"),
        "win_ceiling": wp.get("ceiling"),
        "qual_cap": (f"{int(qc)} by {qbox}={qst}" if qc < 100 else ""),
        "win_engine_score_llm": ewin.get("score"),
        "win_engine_band": ewin.get("band"),
        "win_engine_drivers": joinlines(ewin.get("drivers")),
        "mom_engine_score_llm": emom.get("score"),
        "mom_engine_band": emom.get("band"),
        "mom_engine_drivers": joinlines(emom.get("drivers")),
        "win_reasons_deterministic": reasons(wp),
        "mom_reasons_deterministic": reasons(mm),
        "score_reasons_ai_win": joinlines(air.get("win_position")),
        "score_reasons_ai_momentum": joinlines(air.get("deal_momentum")),
        "signal_extraction": fmt_items(ext.get("signals"), "evidence", cap=12)
                             + (f"\ncoverage={ext.get('coverage')}" if ext.get("coverage") else ""),
        "todo_prospect_requirements": fmt_items(tdo.get("prospect_requirements"), "item"),
        "todo_zycus_commitments": fmt_items(tdo.get("zycus_commitments"), "item"),
        "todo_waiting_on_buyer": fmt_items(tdo.get("waiting_on_buyer"), "item"),
        "todo_best_practices": fmt_items(tdo.get("best_practices"), "item"),
        "suggested_realistic_close": tdo.get("suggested_realistic_close"),
        "summary_24h": (smm.get("headline") or "")
                       + ("".join("\n" + s for s in (smm.get("supporting") or [])))
                       + (f"\n({smm.get('as_of_note')})" if smm.get("as_of_note") else ""),
        "ui_buckets_after_rerun": (f"prospect={len(prospect)} | zycus_commitments={len(commitments)} | "
                                   f"waiting_on_buyer={len(buyer_owed)} | best_practices={len(best_pr)}"),
        "why_only_best_practice_todos_before": DIAG.get(oid, ""),
        "validation_violations": len(viol or []),
    })
    print(f"{name}: verdict={nsv.get('verdict')} | det win {old_hl.get('win_position')}->{hl.get('win_position')} "
          f"mom {old_hl.get('deal_momentum')}->{hl.get('deal_momentum')} | LLM-engine win={ewin.get('score')} "
          f"mom={emom.get('score')} | UI buckets after rerun: prospect={len(prospect)} "
          f"commit={len(commitments)} buyer={len(buyer_owed)} bp={len(best_pr)} | viol={len(viol or [])}")

if rows_out:
    fields = list(rows_out[0].keys())
    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows_out:
            w.writerow(r)
    print(f"\nCSV written: {os.path.abspath(OUT)}  ({len(rows_out)} rows)  — NOTHING written to any DB")
else:
    print("no rows — agent outputs missing")
