"""Backfill ai.exec_f2f (deterministic, DIRECT SOQL — the sweep never pulls the fields this
needs) for EVERY active deal. The verdict asserts to a VP that an in-person executive meeting
happened, so it is inference over free text: Event.Location_Medium__c / Meeting_Sub_Type__c are
100% NULL org-wide. Event.Description + Event.Location are the whole signal and the sweep reads
neither. Attendee truth comes from EventRelation -> Contact; the same meeting is logged 2-3x
(Clari Event + Avoma Event + Task) with DIFFERENT WhoIds on each mirror, so attendees are unioned
across events sharing subject+date before the gate sees them. Parallel SF reads (8 threads).
--apply writes {ai,exec_f2f}."""
import sys, re, json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests, urllib3
from deal_engine_f2f import derive_exec_f2f
from deal_engine_f2f_prep import (EVENT_ORDER_BY, MAX_EVENTS, MAX_TASKS, TASK_ORDER_BY,
                                  clean_description, event_date, event_window_clause,
                                  should_replace, task_window_clause)
from daily_summary import common as C
from daily_summary.common import load_secret, VERIFY, id15
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sec = load_secret(); SB = sec["SUPABASE_URL"].rstrip("/")
KEY = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
REF = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
MGMT = f"https://api.supabase.com/v1/projects/{REF}/database/query"; MTOK = sec["SUPABASE_ACCESS_TOKEN"]
SID, INST = C.sf_login(sec)

DEALS_PER_CHUNK = 40      # one Event/Task/Opp query per chunk, not per deal
IDS_PER_IN = 200          # SOQL IN() batch for the EventRelation / Contact hops
# Per-deal caps come from the shared prep, NOT from local constants: 200/400 here against the
# sweep's 200/200 meant the two writers read a different set of rows on the 19 opps with >200
# events and the 181 with >200 tasks, so whichever ran last won with a different answer.
MAX_EV, MAX_TK = MAX_EVENTS, MAX_TASKS


def _ids(xs):
    """IN(...) literal. Ids are alnum by construction; sanitised anyway (read-only, but SOQL)."""
    return "(" + ",".join("'" + re.sub(r"[^A-Za-z0-9]", "", str(i)) + "'" for i in xs) + ")"


def _key(subject, when):
    """Mirror key: the SAME meeting arrives as a Clari Event, an Avoma Event and a Task, each
    carrying a different slice of the invitee list. Subject+date is what they agree on."""
    return re.sub(r"\s+", " ", str(subject or "")).strip().lower(), str(when or "")[:10]


def pull_chunk(oids):
    """All SOQL for one chunk of deals -> {oid: {"events":[...], "tasks":[...], "next_step":str}}."""
    out = {o: {"events": [], "tasks": [], "next_step": ""} for o in oids}
    inl = _ids(oids)

    evs = C.soql(SID, INST, (
        "SELECT Id,WhatId,Subject,Description,Location,ActivityDate,ActivityDateTime FROM Event "
        f"WHERE WhatId IN {inl} AND {event_window_clause()} {EVENT_ORDER_BY}"))
    by_ev = {}
    for e in evs:
        oid = id15(e.get("WhatId"))
        if oid not in out or len(out[oid]["events"]) >= MAX_EV:
            continue
        # description/date go through the SHARED prep so this and the sweep cannot disagree:
        # Avoma '##' note bodies stripped (they describe the customer's business, not the
        # meeting), ActivityDate preferred over the UTC ActivityDateTime. description_raw is
        # carried alongside because the virtual veto must read the UNCLEANED body — a Teams
        # link below a stripped '##' heading is still a Teams link (SGD Pharma).
        row = {"subject": e.get("Subject"),
               "description": clean_description(e.get("Description")),
               "description_raw": e.get("Description"),
               "location": e.get("Location"),
               "date": event_date(e.get("ActivityDate"), e.get("ActivityDateTime")),
               "attendees": []}
        out[oid]["events"].append(row)
        by_ev[e["Id"]] = (oid, row)

    # EventRelation -> Contact. EventRelation holds buyer Contacts only (no Zycus-side Users),
    # which is exactly the population the seniority test needs.
    ev_ids = list(by_ev)
    rel = []
    for i in range(0, len(ev_ids), IDS_PER_IN):
        rel += C.soql(SID, INST, "SELECT EventId,RelationId FROM EventRelation "
                                 f"WHERE EventId IN {_ids(ev_ids[i:i + IDS_PER_IN])}")
    cids = sorted({r["RelationId"] for r in rel
                   if str(r.get("RelationId") or "").startswith("003")})  # 003 = Contact
    people = {}
    for i in range(0, len(cids), IDS_PER_IN):
        for c in C.soql(SID, INST, "SELECT Id,Name,Title,Email FROM Contact "
                                   f"WHERE Id IN {_ids(cids[i:i + IDS_PER_IN])}"):
            people[id15(c["Id"])] = {"name": c.get("Name"), "title": c.get("Title"),
                                     "email": c.get("Email")}

    # Union across mirrors: collect per (oid, subject, date), then hand every mirror the full set.
    merged = defaultdict(dict)
    for r in rel:
        hit = by_ev.get(r.get("EventId"))
        p = people.get(id15(r.get("RelationId")))
        if hit and p:
            oid, row = hit
            merged[(oid,) + _key(row["subject"], row["date"])][id15(r["RelationId"])] = p
    for oid, row in by_ev.values():
        row["attendees"] = list(merged.get((oid,) + _key(row["subject"], row["date"]), {}).values())

    tks = C.soql(SID, INST, "SELECT WhatId,Subject,Status,ActivityDate FROM Task "
                            f"WHERE WhatId IN {inl} AND {task_window_clause()} {TASK_ORDER_BY}")
    for t in tks:
        oid = id15(t.get("WhatId"))
        if oid in out and len(out[oid]["tasks"]) < MAX_TK:
            out[oid]["tasks"].append({"subject": t.get("Subject"), "status": t.get("Status"),
                                      "date": t.get("ActivityDate")})

    for o in C.soql(SID, INST, f"SELECT Id,Next_Step__c FROM Opportunity WHERE Id IN {inl}"):
        oid = id15(o.get("Id"))
        if oid in out:
            out[oid]["next_step"] = o.get("Next_Step__c") or ""
    return out


def main():
    apply = "--apply" in sys.argv
    # --force bypasses the ratchet. The ratchet exists so a FLAKY run (attendees lost on a
    # failed EventRelation hop) can't demote a stored verdict — but when the DEFINITION
    # changes, demotion is the whole point and the ratchet would freeze the old answer in.
    force = "--force" in sys.argv
    # --only <path-to-json> scopes the run to specific opportunity ids: either a JSON list
    # of 15/18-char ids, or a list of objects carrying an "Id" key.
    only = None
    if "--only" in sys.argv:
        with open(sys.argv[sys.argv.index("--only") + 1], encoding="utf-8") as fh:
            raw = json.load(fh)
        only = {id15(r["Id"] if isinstance(r, dict) else r) for r in raw}
    rows = requests.get(f"{SB}/rest/v1/deal_records",
                        # limit is a ceiling, NOT the book size — 627 active today, and the
                        # inherited "600" in footprints_rebuild_all.py silently drops the tail.
                        # The STORED verdict comes back too: this run must not demote it (see
                        # the ratchet below). `->` on a non-object 'ai' yields NULL, not an error.
                        params={"select": "opp_id,account_name,stored_f2f:record->ai->exec_f2f",
                                "active": "eq.true", "limit": "5000"},
                        headers=H, verify=VERIFY, timeout=180).json()
    names = {id15(r["opp_id"]): r.get("account_name") for r in rows}
    stored = {id15(r["opp_id"]): r.get("stored_f2f") for r in rows}
    oids = sorted(o for o in names if only is None or o in only)
    chunks = [oids[i:i + DEALS_PER_CHUNK] for i in range(0, len(oids), DEALS_PER_CHUNK)]
    print(f"active={len(names)} | targeted={len(oids)} | chunks={len(chunks)} "
          f"x{DEALS_PER_CHUNK}{' | FORCE (ratchet off)' if force else ''}")
    if only is not None and len(oids) != len(only):
        print(f"  NOTE: {len(only) - len(oids)} requested id(s) are not active deals")

    def work(ch):
        try:
            return {o: derive_exec_f2f(events=d["events"], tasks=d["tasks"],
                                       next_step=d["next_step"])
                    for o, d in pull_chunk(ch).items()}
        except Exception as e:  # noqa: BLE001 — one bad chunk must not sink the backfill
            print(f"  ERR chunk {ch[0]}..: {str(e)[:120]}")
            return {}

    out = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for fut in as_completed([ex.submit(work, ch) for ch in chunks]):
            out.update(fut.result())

    counts = defaultdict(int)
    for v in out.values():
        counts[v["status"]] += 1
    near = sum(1 for v in out.values() if v.get("near_miss"))
    print(f"derived {len(out)}/{len(oids)} | done={counts['done']} planned={counts['planned']} "
          f"none={counts['none']} | near_miss={near}")

    # RATCHET — the same done > planned > none rank the sweep applies. This wrote
    # UNCONDITIONALLY, so a run where the EventRelation hop partially failed (attendees lost,
    # so a "done" demotes to a near-miss "planned") silently overwrote a stored "done". A
    # weaker verdict is dropped from the write set; the stored one stands.
    held = {} if force else {o: v for o, v in out.items()
                             if not should_replace(v, stored.get(o))}
    for o in held:
        del out[o]
    if held:
        print(f"\n-- RATCHET: kept {len(held)} stored verdict(s), this run was weaker --")
        for o, v in sorted(held.items(), key=lambda kv: kv[0]):
            print(f"  {(names.get(o) or o)[:34]:34s} | stored="
                  f"{(stored.get(o) or {}).get('status')} > new={v['status']}")
    wcounts = defaultdict(int)
    for v in out.values():
        wcounts[v["status"]] += 1
    wnear = sum(1 for v in out.values() if v.get("near_miss"))
    print(f"writable {len(out)} | done={wcounts['done']} planned={wcounts['planned']} "
          f"none={wcounts['none']} | near_miss={wnear}")
    print("\n-- DONE verdicts (eyeball these before --apply) --")
    for oid, v in sorted(out.items(), key=lambda kv: kv[1].get("date") or "", reverse=True):
        if v["status"] != "done":
            continue
        print(f"  {v['date']}  {(names.get(oid) or oid)[:34]:34s} | {v['exec_name']} "
              f"({v['exec_title']}) | {(v['evidence'] or '')[:70]}")

    if not apply:
        print("\n[DRY RUN] --apply to write {ai,exec_f2f}."); return
    items = list(out.items()); n = 0
    for i in range(0, len(items), 50):
        blob = json.dumps(dict(items[i:i + 50]))
        # GUARDED jsonb_set. Unguarded, this had two ways to lie: jsonb_set on a NULL record
        # returns NULL and WIPES the row, and a record with no 'ai' key is a silent no-op that
        # RETURNING still counts as applied. The case-expression only ADDS an empty 'ai' when
        # one is MISSING, so sibling ai.* keys are never clobbered; re-running is idempotent.
        # A non-object 'ai' (e.g. "ai":"oops") is SKIPPED, not repaired: the old case-expression
        # read `jsonb_typeof(...)='object'` as false and replaced the scalar with {}, destroying
        # whatever it held. Deleting data is not this script's job — such rows are reported
        # instead, and the count below is of rows actually written.
        sql = ("update deal_records d set record = jsonb_set("
               "case when d.record ? 'ai' then d.record "
               "else d.record || jsonb_build_object('ai','{}'::jsonb) end, "
               "'{ai,exec_f2f}', m.value, true), updated_at=now() "
               "from (select key as opp_id, value from jsonb_each($J$" + blob + "$J$::jsonb)) m "
               "where d.opp_id=m.opp_id and d.record is not null and jsonb_typeof(d.record)='object' "
               "and (not d.record ? 'ai' or jsonb_typeof(d.record->'ai')='object') "
               "returning d.opp_id")
        resp = requests.post(MGMT, headers={"Authorization": f"Bearer {MTOK}", "Content-Type": "application/json"},
                             json={"query": sql}, verify=VERIFY, timeout=120)
        n += len(resp.json()) if resp.status_code < 300 else 0
    print(f"  applied {{ai,exec_f2f}}: {n} of {len(items)}"
          + ("  (shortfall = rows with a non-object 'ai', SKIPPED not overwritten)"
             if n < len(items) else ""))


if __name__ == "__main__":
    main()
