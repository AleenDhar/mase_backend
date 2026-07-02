"""Build the 24h structured digest for a scope of deals (default: forecasted book),
entirely locally + read-only. Writes one JSON per opp under
mcp_output/daily_summaries/<date>/ and a consolidated active-only file, and prints
a digest. Narrative summaries are added in a later step (Claude Code, or the
deterministic fallback in extract.deterministic_summary).

Usage:
  python -m daily_summary.run --scope forecasted --window-hours 24
  python -m daily_summary.run --scope all --window-hours 24
  python -m daily_summary.run --ids 006P700000Xl06R,006P7000009O2Ri
"""
from __future__ import annotations
import os, sys, json, argparse, datetime as dt
from collections import defaultdict

try:  # Windows console is cp1252; our summaries contain → and • (UTF-8)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from . import common as C
from . import store as S
from .extract import assemble, deterministic_summary

FORECASTED = {"commit", "best case", "upside", "upside key deal"}


def _load_narratives():
    path = os.path.join(C._REPO, "daily_summary", "narratives.json")
    try:
        d = json.load(open(path, encoding="utf-8"))
        return {k: v for k, v in d.items() if not k.startswith("_")}
    except Exception:
        return {}


def _row(rec, narrative, date_str, gen_iso):
    use_claude = bool(narrative) and rec["has_activity"]
    return {
        "opp_id": rec["opp_id"], "summary_date": date_str,
        "window_start": rec["window_start"], "window_end": rec["window_end"],
        "account_name": rec["account_name"], "opp_name": rec["opp_name"],
        "owner_name": rec["owner_name"], "forecast_category": rec["forecast_category"],
        "stage": rec["stage"], "has_activity": rec["has_activity"],
        "activity_count": rec["counts"]["total"], "counts": rec["counts"],
        "summary": narrative if use_claude else rec["summary_deterministic"],
        "summary_source": "claude" if use_claude else "deterministic",
        "activities": rec["activities"], "movements": rec["movements"],
        "meetings_avoma": rec["meetings_avoma"], "next_step_text": rec["next_step_text"],
        "next_step_changed_at": rec["next_step_changed_at"],
        "generated_at": gen_iso, "updated_at": gen_iso,
    }


def forecasted_meta(sec) -> dict:
    code, rows = C.sb_get(sec, "deal_records?select=opp_id,account_name,opp_name,owner_name,forecast_category,stage&limit=2000")
    if not isinstance(rows, list):
        raise RuntimeError(f"deal_records read failed {code}: {str(rows)[:200]}")
    return {r["opp_id"]: r for r in rows if r.get("opp_id")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=["forecasted", "all"], default="forecasted")
    ap.add_argument("--ids", default="")
    ap.add_argument("--window-hours", type=int, default=24)
    ap.add_argument("--limit", type=int, default=0, help="cap number of opps (debug)")
    ap.add_argument("--write", action="store_true", help="create table + upsert to prod deal DB")
    args = ap.parse_args()

    sec = C.load_secret()
    now = dt.datetime.now(dt.timezone.utc)
    win = now - dt.timedelta(hours=args.window_hours)
    win_iso = C.iso_z(win)
    print(f"[run] now={C.iso_z(now)} window={args.window_hours}h since={win_iso} scope={args.scope}")

    meta_all = forecasted_meta(sec)
    if args.ids:
        ids = [i.strip()[:15] for i in args.ids.split(",") if i.strip()]
    elif args.scope == "all":
        ids = list(meta_all.keys())
    else:
        ids = [oid for oid, r in meta_all.items()
               if (r.get("forecast_category") or "").strip().lower() in FORECASTED]
    if args.limit:
        ids = ids[:args.limit]
    print(f"[run] {len(ids)} opps in scope")
    if not ids:
        return

    sid, instance = C.sf_login(sec)
    IL = C.soql_in(ids, "")

    # batch SF pulls, 24h window
    def q(label, query):
        rows = C.soql(sid, instance, query)
        print(f"[sf] {label}: {len(rows)} rows")
        return rows

    moves = q("moves", f"SELECT OpportunityId, Field, OldValue, NewValue, CreatedDate, CreatedBy.Name FROM OpportunityFieldHistory WHERE OpportunityId IN {IL} AND CreatedDate >= {win_iso}")
    tasks = q("tasks", f"SELECT WhatId, Subject, Status, TaskSubtype, Type, ActivityDate, CreatedDate, LastModifiedDate, CompletedDateTime, Owner.Name FROM Task WHERE WhatId IN {IL} AND (CreatedDate >= {win_iso} OR LastModifiedDate >= {win_iso} OR CompletedDateTime >= {win_iso})")
    events = q("events", f"SELECT WhatId, Subject, ActivityDateTime, CreatedDate, Owner.Name FROM Event WHERE WhatId IN {IL} AND (ActivityDateTime >= {win_iso} OR CreatedDate >= {win_iso})")
    emails = q("emails", f"SELECT RelatedToId, Subject, MessageDate, Incoming, FromAddress FROM EmailMessage WHERE RelatedToId IN {IL} AND MessageDate >= {win_iso}")
    oppf = q("opp-fields", f"SELECT Id, Name, StageName, Amount, CloseDate, ForecastCategoryName, Next_Step__c, Next_Step_Updated_Date_Time__c, LastActivityDate FROM Opportunity WHERE Id IN {IL}")

    g_moves, g_tasks, g_events, g_emails = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    for r in moves:
        g_moves[C.id15(r.get("OpportunityId"))].append(r)
    for r in tasks:
        g_tasks[C.id15(r.get("WhatId"))].append(r)
    for r in events:
        g_events[C.id15(r.get("WhatId"))].append(r)
    for r in emails:
        g_emails[C.id15(r.get("RelatedToId"))].append(r)
    oppf_by = {C.id15(r.get("Id")): r for r in oppf}

    # datalake meetings (best-effort) only for opps that have SF activity in-window
    dl = C.load_datalake()
    active_pre = set(g_moves) | set(g_tasks) | set(g_events) | set(g_emails)

    def avoma_for(oid):
        if not dl:
            return []
        got = C.datalake_get(dl, f"avoma_meetings?select=uuid,subject,start_at,is_call,transcript_ready,notes_ready,crm_opportunity_id&crm_opportunity_id=ilike.{oid}*&start_at=gte.{win_iso}&order=start_at.desc&limit=20")
        return got or []

    date_str = now.strftime("%Y-%m-%d")
    outdir = os.path.join(C._REPO, "mcp_output", "daily_summaries", date_str)
    os.makedirs(outdir, exist_ok=True)

    records, active = [], []
    for oid in ids:
        meta = meta_all.get(oid, {"opp_id": oid})
        rec = assemble(meta, oppf_by.get(oid, {}), g_tasks.get(oid, []), g_events.get(oid, []),
                       g_emails.get(oid, []), g_moves.get(oid, []),
                       avoma_for(oid) if oid in active_pre else [], win, now)
        rec["summary_deterministic"] = deterministic_summary(rec)
        records.append(rec)
        with open(os.path.join(outdir, f"{oid}.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2, default=str)
        if rec["has_activity"]:
            active.append(rec)

    active.sort(key=lambda r: r["counts"]["total"], reverse=True)
    with open(os.path.join(outdir, "_active.json"), "w", encoding="utf-8") as f:
        json.dump(active, f, indent=2, default=str)

    if args.write:
        narr = _load_narratives()
        gen_iso = C.iso_z(now)
        rows = [_row(r, narr.get(r["opp_id"]), date_str, gen_iso) for r in records]
        S.ensure_table(sec)
        n = S.upsert_summaries(sec, rows)
        claude_n = sum(1 for row in rows if row["summary_source"] == "claude")
        print(f"[write] upserted {n} rows to deal_daily_summaries ({claude_n} Claude-authored, {len(active)} active)")

    print(f"\n[run] {len(records)} built, {len(active)} with activity → {outdir}")
    print("\n=== ACTIVE DEALS (last %dh) ===" % args.window_hours)
    for r in active:
        c = r["counts"]
        print(f"\n### {r['account_name']}  ({r['owner_name']})  [{r['forecast_category']}]  total={c['total']}")
        print("   " + r["summary_deterministic"].replace("\n", "\n   "))


if __name__ == "__main__":
    main()
