"""CEO-attention v1 FETCH — build a 14-day-surgical evidence pack per opp with
win_position >= 40. Two determinations downstream:
  SUPPORT  = CEO must ACT (4 levers: pricing/product/presales_resources/exec_connect).
  MONITOR  = CEO should WATCH (T1 our-side deliverable slip, T2 large-deal slowdown
             >=$250K, T3 competition out-delivering) — every flag MUST be anchored to
             a signal dated within the LAST 14 DAYS. Buyer-dependent waits are softened.

Read-only, local ($0). Writes ceo_attention/<opp>.json + _index.json.
"""
from __future__ import annotations
import os, json, sys, datetime as dt
from collections import defaultdict
from daily_summary.common import (load_secret, load_datalake, sf_login, soql,
                                  datalake_get, id15, strip_html, parse_sf, sb_get, VERIFY)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WIN_FLOOR = 40.0
LARGE = 250000.0
WINDOW_DAYS = 14
FC = {"commit", "best case", "upside key deal"}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ceo_attention")
os.makedirs(OUT, exist_ok=True)


def _n(r, *p):
    cur = r
    for k in p[:-1]:
        cur = (cur or {}).get(k) or {}
    return (cur or {}).get(p[-1])


def _within(ts, since):
    d = parse_sf(ts)
    return bool(d and d >= since)


def eligible_records(sec):
    code, rows = sb_get(sec, "deal_records?select=opp_id,account_name,owner_name,forecast_category,amount,stage,close_date,active,record&active=eq.true")
    out = []
    for r in rows if isinstance(rows, list) else []:
        hl = ((r.get("record") or {}).get("ai") or {}).get("deal_scores", {}).get("headline", {}) or {}
        try:
            w = float(hl.get("win_position"))
        except (TypeError, ValueError):
            continue
        if w >= WIN_FLOOR:
            r["_win"], r["_mom"] = w, hl.get("deal_momentum")
            out.append(r)
    return out


def deliverables(ai):
    """Zycus-owed (we_promised) vs buyer-dependent, from the record. Each tagged."""
    ir = ai.get("implicit_requirements") or {}
    ours, buyer = [], []
    if isinstance(ir, dict) and ("we_promised" in ir or "buyer_dependent" in ir):
        for it in ((ir.get("we_promised") or {}).get("items") or []):
            ours.append({"text": it.get("deliverable") or it.get("commitment") or it.get("inferred_need"),
                         "due": it.get("due"), "date": it.get("date"), "status": it.get("status")})
        for it in ((ir.get("buyer_dependent") or {}).get("items") or []):
            buyer.append({"text": it.get("deliverable") or it.get("commitment"),
                          "due": it.get("due"), "date": it.get("date"), "who": it.get("who") or "Buyer"})
    # legacy open_deliverables
    od = ai.get("open_deliverables") or {}
    for it in (od.get("items") or []) if isinstance(od, dict) else []:
        who = str(it.get("who") or "").lower()
        tgt = buyer if ("buyer" in who or "customer" in who) else ours
        tgt.append({"text": it.get("commitment") or it.get("deliverable"),
                    "due": it.get("due"), "date": it.get("date"), "status": it.get("status")})
    return ours, buyer


def main():
    sec = load_secret()
    dl = load_datalake()
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=WINDOW_DAYS)
    recs = eligible_records(sec)
    if "--non-forecasted" in sys.argv:
        recs = [r for r in recs if (r.get("forecast_category") or "").strip().lower() not in FC]
        print(f"[scope] NON-FORECASTED eligible only (forecasted already got fresh CEO from the sweep)")
    ids = [id15(r["opp_id"]) for r in recs]
    print(f"win>=40 eligible: {len(recs)} | 14d window since {since.date()}")
    if not ids:
        return
    sid, inst = sf_login(sec)
    IL = "(" + ",".join("'" + i + "'" for i in ids) + ")"

    # --- batched 14-day SF pulls -------------------------------------------------
    def q(label, query):
        rows = soql(sid, inst, query)
        print(f"  [sf] {label}: {len(rows)}")
        return rows

    moves = q("field-history", f"SELECT OpportunityId, Field, OldValue, NewValue, CreatedDate, CreatedBy.Name FROM OpportunityFieldHistory WHERE OpportunityId IN {IL} AND CreatedDate >= LAST_N_DAYS:{WINDOW_DAYS}")
    tasks = q("tasks", f"SELECT WhatId, Subject, Type, Status, CreatedDate, LastModifiedDate, CompletedDateTime, Owner.Name FROM Task WHERE WhatId IN {IL} AND (CreatedDate >= LAST_N_DAYS:{WINDOW_DAYS} OR LastModifiedDate >= LAST_N_DAYS:{WINDOW_DAYS})")
    events = q("events", f"SELECT WhatId, Subject, ActivityDateTime, CreatedDate, Owner.Name FROM Event WHERE WhatId IN {IL} AND (ActivityDateTime >= LAST_N_DAYS:{WINDOW_DAYS} OR CreatedDate >= LAST_N_DAYS:{WINDOW_DAYS})")
    oppf = q("opp-fields", f"SELECT Id, LastActivityDate, Next_Step__c, Next_Step_Updated_Date_Time__c FROM Opportunity WHERE Id IN {IL}")

    g_moves, g_tasks, g_events = defaultdict(list), defaultdict(list), defaultdict(list)
    for r in moves:
        g_moves[id15(r.get("OpportunityId"))].append(r)
    for r in tasks:
        g_tasks[id15(r.get("WhatId"))].append(r)
    for r in events:
        g_events[id15(r.get("WhatId"))].append(r)
    oppf_by = {id15(r.get("Id")): r for r in oppf}

    EMAIL = ("[clari - email", "[outreach] [email", "email received", "email sent", "[in]", "[out]")
    MEET = ("[clari - meeting", "avoma -", "meeting", "demo", "workshop", "onsite")

    def classify(subj):
        s = (subj or "").lower()
        if any(m in s for m in EMAIL):
            return "email"
        if any(m in s for m in MEET):
            return "meeting"
        return "task"

    packs = []
    for r in recs:
        oid = id15(r["opp_id"])
        ai = (r.get("record") or {}).get("ai") or {}
        amount = r.get("amount") or 0
        # recent movements (14d) — close-date push / stage regress are slowdown signals
        rmoves = []
        for h in g_moves.get(oid, []):
            if _within(h.get("CreatedDate"), since):
                rmoves.append({"field": h.get("Field"), "old": h.get("OldValue"), "new": h.get("NewValue"),
                               "at": (h.get("CreatedDate") or "")[:10], "by": _n(h, "CreatedBy", "Name")})
        # recent activities (14d), split direction
        racts = []
        for t in g_tasks.get(oid, []):
            eff = t.get("CompletedDateTime") or t.get("LastModifiedDate") or t.get("CreatedDate")
            if not _within(eff, since):
                continue
            subj = t.get("Subject") or ""
            sl = subj.lower()
            direction = "in" if ("[in]" in sl or "received" in sl) else ("out" if ("[out]" in sl or "sent" in sl) else None)
            racts.append({"kind": classify(subj), "subject": subj[:120], "direction": direction,
                          "at": (eff or "")[:10], "owner": _n(t, "Owner", "Name")})
        for e in g_events.get(oid, []):
            eff = e.get("ActivityDateTime") or e.get("CreatedDate")
            if _within(eff, since):
                racts.append({"kind": "meeting", "subject": (e.get("Subject") or "")[:120],
                              "direction": None, "at": (eff or "")[:10], "owner": _n(e, "Owner", "Name")})
        # recent Avoma calls (14d) with a short competitive-relevant snippet
        rcalls = []
        if dl:
            ms = datalake_get(dl, f"avoma_meetings?crm_opportunity_id=ilike.{oid}*&start_at=gte.{since.strftime('%Y-%m-%dT%H:%M:%SZ')}&select=uuid,subject,start_at&order=start_at.desc&limit=8") or []
            for m in ms:
                tr = datalake_get(dl, f"avoma_transcripts?meeting_uuid=eq.{m['uuid']}&select=transcript_text&limit=1") or []
                txt = (tr[0].get("transcript_text") if tr else "") or ""
                rcalls.append({"subject": m.get("subject"), "date": (m.get("start_at") or "")[:10],
                               "snippet": txt[:1500]})
        of = oppf_by.get(oid, {})
        # compute days-since-last-activity on DATES (avoid tz naive/aware mix)
        def _d(x):
            p = parse_sf(x)
            return p.date() if p else None
        act_dates = [_d(a["at"]) for a in racts if a.get("at")]
        act_dates += [_d(of.get("LastActivityDate"))]
        act_dates = [d for d in act_dates if d]
        newest = max(act_dates) if act_dates else None
        days_since = (now.date() - newest).days if newest else None

        ours, buyer = deliverables(ai)
        ns_recent = _within(of.get("Next_Step_Updated_Date_Time__c"), since)

        pack = {
            "opp_id": oid, "account": r.get("account_name"), "owner": r.get("owner_name"),
            "amount": amount, "is_large": amount >= LARGE,
            "forecast_category": r.get("forecast_category"),
            "is_forecasted": (r.get("forecast_category") or "").strip().lower() in FC,
            "stage": r.get("stage"), "close_date": r.get("close_date"),
            "win": r["_win"], "mom": r["_mom"],
            "window_days": WINDOW_DAYS, "as_of": now.strftime("%Y-%m-%d"),
            "days_since_last_activity": days_since,
            "next_step_updated_in_window": ns_recent,
            "next_step_text": strip_html(of.get("Next_Step__c"))[:400],
            "recent_movements_14d": rmoves,
            "recent_activities_14d": racts,
            "recent_calls_14d": rcalls,
            "our_open_deliverables": ours,        # Zycus owes — T1 candidates
            "buyer_dependent_items": buyer,       # soften — we wait on the buyer
            "competitive_position": ai.get("competitive_position") or {},
            "champion_strength": ai.get("champion_strength") or {},
            "meddpicc_economic_buyer": (ai.get("meddpicc") or {}).get("economic_buyer") or {},
            "recommended_moves": [m.get("action") for m in ((ai.get("recommended_moves") or {}).get("items") or [])[:3]],
        }
        packs.append({"opp_id": oid, "account": r.get("account_name")})
        json.dump(pack, open(os.path.join(OUT, f"{oid}.json"), "w", encoding="utf-8"), indent=2, default=str)

    json.dump(packs, open(os.path.join(OUT, "_index.json"), "w"), indent=2)
    # quick signal tally
    has_move = sum(1 for r in recs if g_moves.get(id15(r["opp_id"])))
    print(f"\nwrote {len(packs)} packs -> {OUT} | {has_move} have 14d movements")


if __name__ == "__main__":
    main()
