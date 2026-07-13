"""Claude-Code deal sweep — reproduces the production sweep OFF the prod Anthropic
API ($0). Network is done with local Zscaler-friendly helpers; the LLM analysis is
produced by a Claude-Code subagent; the DETERMINISTIC POST-PROCESSING reuses the
REAL modules (deal_engine_validation / scoring / cro / ceo / _roster_from_sfdc), so
the guardrails + scores + CEO are byte-for-byte the production logic.

Flow per opp:
  prefetch(opp)  -> raw SF + Avoma + prior record -> writes cc_work/<opp>.msg  (the subagent input)
  [subagent]     -> emits canonical JSON          -> cc_work/<opp>.json
  postprocess()  -> real guardrails+scores+ceo    -> final record             -> (dry-run compare | upsert)

This module provides prefetch() + postprocess(); the subagent step is driven by a
Workflow. Import-safe: heavy prod deps (fastmcp/httpx-verify) are avoided.
"""
from __future__ import annotations
import os, re, json, sys, datetime as dt
from daily_summary.common import (load_secret, load_datalake, sf_login, soql,
                                   datalake_get, id15, strip_html, sb_get)

WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cc_work")
os.makedirs(WORK, exist_ok=True)


def load_env():
    """Inject creds the real post-processing modules may read from env."""
    sec = load_secret()
    for k in ("SF_USERNAME", "SF_PASSWORD", "SF_SECURITY_TOKEN", "SF_DOMAIN",
              "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_KEY"):
        if sec.get(k):
            os.environ[k] = sec[k]
    os.environ.setdefault("DEAL_ENGINE_AI_SCORING", "")   # force the PURE scorer, no LLM
    return sec


# --- prefetch (local, Zscaler-friendly) -------------------------------------
def _n(r, *path):
    cur = r
    for p in path[:-1]:
        cur = (cur or {}).get(p) or {}
    return (cur or {}).get(path[-1])


def prefetch(sid, inst, sec, dl, opp_id):
    oid = id15(opp_id)
    # authoritative opp (hard facts) — the _map_opps-ish shape the post-processing expects
    o = soql(sid, inst, "SELECT Id, Name, StageName, Amount, CloseDate, ForecastCategoryName, "
             "CreatedDate, LastModifiedDate, LastActivityDate, Next_Step__c, Next_Step_History__c, "
             "Description, Competitors__c, Products__c, Type, Account.Name, Account.Website, "
             "Account.Industry, Account.BillingCountry, Owner.Name, Owner.Title, Owner.Manager.Name, "
             # AIS_Score__c/Status/Why REMOVED 2026-07-09 — they do not exist on Opportunity in
             # this org and 400 the whole query (matches the deal_engine_sweep._OPP_SELECT_FIELDS fix).
             "Qualified_Submission_Date__c "
             f"FROM Opportunity WHERE Id='{opp_id}'")
    o = o[0] if o else {}
    opp = {
        "id": oid, "name": o.get("Name"), "account": _n(o, "Account", "Name"),
        "account_id": None, "website": _n(o, "Account", "Website"),
        "account_industry": _n(o, "Account", "Industry"),
        "billing_country": _n(o, "Account", "BillingCountry"),
        "owner_name": _n(o, "Owner", "Name"), "owner_title": _n(o, "Owner", "Title"),
        "manager_name": _n(o, "Owner", "Manager", "Name"),
        "stage": o.get("StageName"), "amount": o.get("Amount"),
        "close_date": o.get("CloseDate"), "forecast_category": o.get("ForecastCategoryName"),
        "created_date": o.get("CreatedDate"), "last_modified_date": o.get("LastModifiedDate"),
        "last_activity_date": o.get("LastActivityDate"),
        "qualified_date": o.get("Qualified_Submission_Date__c"),
        "next_step": strip_html(o.get("Next_Step__c")), "products": o.get("Products__c"),
        "competitor": o.get("Competitors__c"),
        # AIS_Score__c/Status/Why are NOT selected (they do not exist on Opportunity in
        # this org and 400 the whole query — the 2026-07-09 book-wipe). Hard-None so the
        # record shape is unchanged. NEVER re-add them to the SOQL above.
        "ais_score": None, "ais_status": None, "ais_why": None,
    }
    # contacts (OpportunityContactRole)
    roles = soql(sid, inst, "SELECT Contact.Name, Contact.Title, Contact.Email, Role, IsPrimary "
                 f"FROM OpportunityContactRole WHERE OpportunityId='{opp_id}'")
    contacts = [{"name": _n(r, "Contact", "Name"), "title": _n(r, "Contact", "Title"),
                 "email": _n(r, "Contact", "Email"), "role": r.get("Role"),
                 "is_primary": r.get("IsPrimary")} for r in roles if _n(r, "Contact", "Name")]
    buyer = {"contacts": contacts, "account_name": opp["account"], "task_contacts": []}
    # tasks + events (90d)
    tasks = soql(sid, inst, "SELECT Subject, Type, Status, ActivityDate, CreatedDate, Description, Who.Name "
                 f"FROM Task WHERE WhatId='{opp_id}' AND CreatedDate >= LAST_N_DAYS:120 ORDER BY CreatedDate DESC LIMIT 80")
    events = soql(sid, inst, "SELECT Subject, ActivityDateTime, CreatedDate, Description, Who.Name "
                  f"FROM Event WHERE WhatId='{opp_id}' AND CreatedDate >= LAST_N_DAYS:120 ORDER BY CreatedDate DESC LIMIT 40")
    for t in tasks:
        nm = _n(t, "Who", "Name")
        if nm and nm not in buyer["task_contacts"]:
            buyer["task_contacts"].append(nm)
    # field history (movements)
    fh = soql(sid, inst, "SELECT Field, OldValue, NewValue, CreatedDate, CreatedBy.Name "
              f"FROM OpportunityFieldHistory WHERE OpportunityId='{opp_id}' ORDER BY CreatedDate DESC LIMIT 30")
    # Avoma transcripts (datalake)
    avoma = []
    if dl:
        ms = datalake_get(dl, f"avoma_meetings?crm_opportunity_id=ilike.{oid}*&select=uuid,subject,start_at,is_internal&state=in.(completed,not_recorded)&order=start_at.desc&limit=25") or []
        # 200K transcript budget (was 80K): the 80K cap read only 2 of 17 Consumer Cellular calls
        # and cost the local run ~18 win points vs the cloud read — enriched runs need the history.
        budget = 200000
        for m in ms:
            tr = datalake_get(dl, f"avoma_transcripts?meeting_uuid=eq.{m['uuid']}&select=transcript_text&limit=1") or []
            txt = (tr[0].get("transcript_text") if tr else "") or ""
            ins = datalake_get(dl, f"avoma_insights?uuid=eq.{m['uuid']}&select=ai_notes_text&limit=1") or []
            notes = (ins[0].get("ai_notes_text") if ins else "") or ""
            ex = ""
            if txt and budget > 0:
                ex = txt[:budget]; budget -= len(ex)
            avoma.append({"subject": m.get("subject"), "date": (m.get("start_at") or "")[:10],
                          "notes": notes[:4000], "transcript_excerpt": ex})
    # prior record (living memory)
    code, rows = sb_get(sec, f"deal_records?opp_id=eq.{oid}&select=record")
    existing = (rows[0]["record"] if isinstance(rows, list) and rows else {}) or {}
    return {"opp": opp, "buyer": buyer, "tasks": tasks, "events": events,
            "field_history": fh, "avoma": avoma, "existing": existing}


# --- user message (faithful in DATA; the subagent gets the 76K system prompt) -
def build_user_msg(pf):
    opp, buyer = pf["opp"], pf["buyer"]
    L = [f"Sweep Salesforce Opportunity Id `{opp['id']}` (account: {opp.get('account')}, name: {opp.get('name')}). "
         "Follow your system prompt end-to-end and emit the canonical record JSON. Output JSON only, no preamble.",
         "\n=== AUTHORITATIVE SALESFORCE FACTS (ground truth — use verbatim for hard.*) ==="]
    for k in ("name", "account", "account_industry", "billing_country", "owner_name", "owner_title",
              "manager_name", "stage", "forecast_category", "amount", "close_date", "created_date",
              "qualified_date", "last_activity_date", "products", "competitor", "ais_score",
              "ais_status", "ais_why"):
        if opp.get(k) not in (None, ""):
            L.append(f"  {k}: {opp[k]}")
    L.append(f"  next_step: {opp.get('next_step')}")
    L.append(f"\nOwner's manager (GROUND TRUTH; emit NO manager_name field): {opp.get('manager_name')}")
    L.append("\n=== OPPORTUNITY CONTACT ROLES (the ONLY real people; names+titles are canonical) ===")
    for c in buyer["contacts"]:
        L.append(f"  - {c['name']} | {c.get('title') or ''} | {c.get('email') or ''} | role={c.get('role') or ''}"
                 + (" | PRIMARY" if c.get("is_primary") else ""))
    if buyer["task_contacts"]:
        L.append("  (task/activity contacts: " + ", ".join(buyer["task_contacts"][:15]) + ")")
    L.append("\n=== SALESFORCE ACTIVITIES (last 120d) ===")
    for t in pf["tasks"][:40]:
        d = (t.get("ActivityDate") or (t.get("CreatedDate") or "")[:10])
        L.append(f"  [task {d}] {t.get('Type') or ''} | {t.get('Subject') or ''}"
                 + (f" — {strip_html(t.get('Description'))[:200]}" if t.get("Description") else ""))
    for e in pf["events"][:20]:
        d = (e.get("ActivityDateTime") or e.get("CreatedDate") or "")[:10]
        L.append(f"  [event {d}] {e.get('Subject') or ''}")
    if pf["field_history"]:
        L.append("\n=== FIELD HISTORY (movements) ===")
        for h in pf["field_history"][:20]:
            L.append(f"  {(h.get('CreatedDate') or '')[:10]} {h.get('Field')}: {h.get('OldValue')} -> {h.get('NewValue')} (by {_n(h,'CreatedBy','Name')})")
    L.append("\n=== AVOMA CALLS (verbatim transcripts + notes — the primary evidence) ===")
    for a in pf["avoma"]:
        L.append(f"\n--- CALL: {a['subject']} ({a['date']}) ---")
        if a["notes"]:
            L.append("Notes / summary: " + a["notes"])
        if a["transcript_excerpt"]:
            L.append("Transcript (verbatim): " + a["transcript_excerpt"])
    if not pf["avoma"]:
        L.append("  (no Avoma calls found for this opp)")
    # prior record — living memory carry-forward
    ex_ai = (pf["existing"].get("ai") or {})
    if ex_ai:
        prior = {k: ex_ai.get(k) for k in ("north_star_verdict", "competitive_position",
                 "champion_strength", "stakeholder_map", "meddpicc") if ex_ai.get(k)}
        L.append("\n=== PRIOR CANONICAL RECORD (living memory — ACCRETE, never regress; keep dated facts) ===")
        L.append(json.dumps(prior, default=str)[:12000])
    return "\n".join(L)


def all_active_opp_ids():
    sec = load_secret()
    base, key = sec["SUPABASE_URL"].rstrip("/"), (sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY"))
    import requests
    from daily_summary.common import VERIFY
    rows = requests.get(f"{base}/rest/v1/deal_records",
                        params={"select": "opp_id", "active": "eq.true", "order": "opp_id.asc"},
                        headers={"apikey": key, "Authorization": f"Bearer {key}"},
                        verify=VERIFY, timeout=120).json()
    return [r["opp_id"] for r in rows if r.get("opp_id")]


def prefetch_to_files(opp_ids, resume=True, workers=12):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    load_env()
    sec = load_secret(); dl = load_datalake()
    sid, inst = sf_login(sec)   # one shared read session (thread-safe for GETs)
    todo = [id15(o) for o in opp_ids
            if not (resume and os.path.exists(os.path.join(WORK, f"{id15(o)}.msg"))
                    and os.path.getsize(os.path.join(WORK, f"{id15(o)}.msg")) > 500)]
    done_pre = len(opp_ids) - len(todo)
    counter = {"n": 0, "err": 0}
    lock = threading.Lock()

    def work(opp):
        try:
            pf = prefetch(sid, inst, sec, dl, opp)
            msg = build_user_msg(pf)
            open(os.path.join(WORK, f"{opp}.msg"), "w", encoding="utf-8").write(msg)
            json.dump({"opp": pf["opp"], "buyer": pf["buyer"], "existing": pf["existing"]},
                      open(os.path.join(WORK, f"{opp}.ctx.json"), "w", encoding="utf-8"), default=str)
            with lock:
                counter["n"] += 1; n = counter["n"]
            if n % 10 == 0 or n <= 3:
                print(f"  [{n}/{len(todo)}] {opp} | {(pf['opp']['account'] or '')[:22]:22} | msg={len(msg)} | {len(pf['avoma'])} calls", flush=True)
        except Exception as e:
            with lock:
                counter["err"] += 1
            print(f"  {opp} PREFETCH FAILED: {type(e).__name__}: {str(e)[:90]}", flush=True)

    print(f"prefetch: {len(todo)} to fetch ({done_pre} already done), {workers} workers", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(as_completed([ex.submit(work, o) for o in todo]))
    print(f"\nprefetch done: {counter['n']} new, {done_pre} already had .msg, {counter['err']} failed", flush=True)
    return counter


# --- post-processing: the REAL deterministic chain (guardrails+scores+ceo) ---
def postprocess(parsed, opp, buyer, existing):
    """Apply the production post-processing to a subagent-produced record. Reuses
    the real modules so the guardrails/scores/CEO are byte-for-byte production."""
    import deal_engine_validation as V
    import deal_engine_scoring as SC
    import deal_engine_cro as CRO
    import deal_engine_ceo as CEO
    import deal_engine_sweep as S
    if not isinstance(parsed, dict):
        return None
    parsed.setdefault("ai", {})
    hard = parsed.setdefault("hard", {})
    parsed["opp_id"] = id15(opp["id"])
    parsed["swept_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    allow = V.build_people_allowlist(buyer, existing)

    # PIN GUARD (matches production analyze_one): a human-corrected deal
    # (ai.pinned==true) freezes its deal_scores + stakeholder_map + ceo_intervention.
    # Hard facts still refresh below; the curated analysis is carried forward verbatim
    # and the pin re-stamped so it survives the upsert. NEVER clobber a correction.
    _prior_ai = existing.get("ai") if isinstance(existing, dict) else None
    _pinned = bool(isinstance(_prior_ai, dict) and _prior_ai.get("pinned"))
    if _pinned:
        for k in ("deal_scores", "stakeholder_map", "ceo_intervention"):
            if _prior_ai.get(k) is not None:
                parsed["ai"][k] = _prior_ai[k]
        parsed["ai"]["pinned"] = True
        if _prior_ai.get("pinned_at"):
            parsed["ai"]["pinned_at"] = _prior_ai["pinned_at"]

    # A) hard facts server-owned + manager + people sanitize + provenance
    V.apply_sf_hard_facts(hard, opp, authoritative=True)
    V.reassert_manager(hard, opp)
    V.sanitize_people(parsed["ai"], allow)
    V.scrub_record(parsed)
    V.stamp_fact_sources(hard, opp)

    # B) validate; last-resort repair on violations
    viol = V.validate_record(parsed, sf_facts=opp, contact_roles=buyer.get("contacts"),
                             prior_names=V._sourced_names_in_record(existing))
    if viol:
        V.sanitize_failed_record(parsed, viol, opp, allowlist=allow)

    # C) free-text guardrails (meddpicc + title/name fidelity)
    V.sanitize_meddpicc(parsed["ai"], allow, opp)
    V.sanitize_title_claims(parsed["ai"], V.build_contact_titles(buyer), allow, opp)

    # D-F) scoring / roster / CEO — SKIPPED for a pinned deal (its curated
    # deal_scores + stakeholder_map + ceo_intervention were carried forward above).
    if not _pinned:
        # D) scores (PURE, no LLM) + CRO panel
        try:
            sc = SC.compute_deal_scores(parsed)
            if sc:
                parsed["ai"]["deal_scores"] = sc
                panel = CRO.build_cro_panel(parsed)
                if panel:
                    parsed["ai"]["deal_scores"]["cro_panel"] = panel
        except Exception as e:
            print("   [scores] skipped:", e)

        # E) SFDC-anchored roster
        try:
            parsed["ai"] = S._roster_from_sfdc(parsed["ai"], buyer, existing)
        except Exception as e:
            print("   [roster] skipped:", e)

        # F) native CEO (win>60 floor + AI discriminator + sanitize)
        CEO.finalize_ceo_intervention(parsed, opp, buyer, prior_ai=_prior_ai, allowlist=allow)
    return parsed, viol


def postprocess_from_files(opp_ids, upsert=False, skip_done=False):
    load_env()
    sec = load_secret()
    from opportunity_analyzer import _extract_json
    results = []
    for opp in opp_ids:
        oid = id15(opp)
        jpath = os.path.join(WORK, f"{oid}.json")
        donep = os.path.join(WORK, f"{oid}.done")
        if skip_done and os.path.exists(donep):
            continue
        if not os.path.exists(jpath) or os.path.getsize(jpath) < 50:
            continue
        try:
            raw = open(jpath, encoding="utf-8").read()
            parsed = _extract_json(raw) if not raw.strip().startswith("{") else json.loads(raw)
            if not isinstance(parsed, dict) or not parsed.get("ai"):
                print(f"  {oid}: unparseable/empty subagent JSON — skip"); continue
            ctx = json.load(open(os.path.join(WORK, f"{oid}.ctx.json"), encoding="utf-8"))
            rec, viol = postprocess(parsed, ctx["opp"], ctx["buyer"], ctx["existing"])
        except Exception as e:
            print(f"  {oid}: postprocess FAILED: {type(e).__name__}: {str(e)[:100]}"); continue
        json.dump(rec, open(os.path.join(WORK, f"{oid}.final.json"), "w", encoding="utf-8"), indent=2, default=str)
        ai = rec.get("ai") or {}
        hl = (ai.get("deal_scores") or {}).get("headline") or {}
        ci = ai.get("ceo_intervention") or {}
        sm = (ai.get("stakeholder_map") or {}).get("items") or []
        nsv = ai.get("north_star_verdict") or {}
        results.append({"opp": oid, "account": ctx["opp"].get("account"),
                        "win": hl.get("win_position"), "mom": hl.get("deal_momentum"),
                        "north_star": nsv.get("verdict") or nsv.get("read"),
                        "ceo_needed": ci.get("needed"),
                        "stakeholders": [s.get("name") for s in sm[:6]],
                        "violations": len(viol)})
        if upsert:
            from daily_summary.common import sb_upsert
            H = rec.get("hard") or {}
            # EXACT row shape of deal_engine_store.upsert_record (all flat mirror
            # columns) so the CC-sweep write is identical to production. `active`
            # is intentionally NOT sent — PostgREST merge-duplicates leaves it
            # untouched on an existing row (never demotes a live deal).
            row = {"opp_id": oid, "owner_name": H.get("owner_name"),
                   "account_name": H.get("account_name"), "opp_name": H.get("opp_name"),
                   "stage": H.get("stage"), "forecast_category": H.get("forecast_category"),
                   "amount": H.get("amount"), "close_date": H.get("close_date") or None,
                   "qualified_date": H.get("qualified_date") or None,
                   "last_activity_date": H.get("last_activity_date") or None,
                   "forecast_critical": bool(rec.get("forecast_critical")),
                   "analysis_confidence": rec.get("analysis_confidence"),
                   "swept_at": rec.get("swept_at"), "record": rec,
                   "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            sb_upsert(sec, "deal_records", [row], on_conflict="opp_id")
            open(donep, "w").write(rec.get("swept_at") or "1")  # checkpoint marker
    return results


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "prefetch"
    ids = sys.argv[2:] or ["006P700000J71MD"]
    if mode == "prefetch-all":
        load_env()
        allids = all_active_opp_ids()
        print(f"prefetching ALL {len(allids)} active opps (resume-safe)...", flush=True)
        prefetch_to_files([id15(x) for x in allids])
    elif mode == "list-pending":
        # opps that have a .msg (prefetched) but no subagent .json yet
        import glob as _g
        msgs = {os.path.basename(p)[:-4] for p in _g.glob(os.path.join(WORK, "*.msg"))}
        jsons = {os.path.basename(p)[:-5] for p in _g.glob(os.path.join(WORK, "*.json")) if ".ctx." not in p and ".final." not in p}
        pending = sorted(msgs - jsons)
        print(json.dumps(pending))
    elif mode == "prefetch":
        prefetch_to_files(ids)
    elif mode == "checkpoint":
        # incremental: post-process + upsert every completed subagent JSON not yet
        # upserted (marker-based). Safe to run repeatedly while the sweep workflow runs.
        import glob as _g
        allids = sorted({os.path.basename(p)[:-5] for p in _g.glob(os.path.join(WORK, "*.json"))
                         if ".ctx." not in p and ".final." not in p})
        res = postprocess_from_files(allids, upsert=True, skip_done=True)
        done_total = len(_g.glob(os.path.join(WORK, "*.done")))
        print(f"\n[checkpoint] upserted {len(res)} new this pass | {done_total}/457 total done")
    elif mode in ("post", "post-upsert"):
        for r in postprocess_from_files(ids, upsert=(mode == "post-upsert")):
            print(f"\n{r['account']} ({r['opp']}): win={r['win']} mom={r['mom']} | "
                  f"north_star={r['north_star']} | ceo_needed={r['ceo_needed']} | violations={r['violations']}")
            print("   stakeholders:", r["stakeholders"])
