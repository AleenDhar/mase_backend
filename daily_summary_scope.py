"""READ-ONLY scope/measurement: define the forecasted set and measure how much
activity actually lands in the last 24h / 72h / 7d, so we pick a sensible window
and pick demo examples that have real content. Writes nothing.
"""
import datetime as dt
from daily_summary_probe import load_secret, sf_login, soql, sb_get


def parse_sf(ts):
    if not ts:
        return None
    ts = ts.replace("+0000", "+00:00").replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(ts)
    except Exception:
        return None


def in_list(ids):
    return "(" + ",".join("'" + i + "'" for i in ids) + ")"


def main():
    sec = load_secret()
    now = dt.datetime.now(dt.timezone.utc)
    t24, t72, t7 = now - dt.timedelta(hours=24), now - dt.timedelta(hours=72), now - dt.timedelta(days=7)
    print("now(UTC):", now.strftime("%Y-%m-%dT%H:%M:%SZ"))

    # --- all deal_records (flat cols only; cheap) ------------------------------
    code, rows = sb_get(sec, "deal_records?select=opp_id,account_name,owner_name,forecast_category,stage&limit=2000")
    if not isinstance(rows, list):
        print("deal_records error:", code, str(rows)[:300]); return
    print("deal_records rows:", len(rows), "(status", code, ")")

    dist = {}
    for r in rows:
        dist[r.get("forecast_category")] = dist.get(r.get("forecast_category"), 0) + 1
    print("\nforecast_category distribution:")
    for k, v in sorted(dist.items(), key=lambda x: -x[1]):
        print(f"   {str(k):24} {v}")

    FORECASTED = {"commit", "best case", "upside", "upside key deal"}
    fc = [r for r in rows if (r.get("forecast_category") or "").strip().lower() in FORECASTED]
    print(f"\nforecasted deals (Commit/Best Case/Upside): {len(fc)}")
    ids = [r["opp_id"] for r in fc if r.get("opp_id")]
    by_id = {r["opp_id"]: r for r in fc}
    if not ids:
        print("!! no forecasted deals matched; check the category strings above")
        return

    sid, instance = sf_login(sec)
    print("sf login OK:", instance)

    # --- activity/movement across the whole forecasted set, last 7d -----------
    IL = in_list(ids)
    counts = {i: {"24": 0, "72": 0, "7": 0} for i in ids}
    detail = {i: {"tasks": 0, "events": 0, "emails": 0, "moves": 0} for i in ids}

    def bump(opp, ts, kind):
        opp = (opp or "")[:15]  # SF returns 18-char ids; deal_records keys are 15-char
        d = parse_sf(ts)
        if not d or opp not in counts:
            return
        if d >= t7:
            counts[opp]["7"] += 1
            detail[opp][kind] += 1
        if d >= t72:
            counts[opp]["72"] += 1
        if d >= t24:
            counts[opp]["24"] += 1

    q_moves = f"SELECT OpportunityId, Field, CreatedDate FROM OpportunityFieldHistory WHERE OpportunityId IN {IL} AND CreatedDate >= LAST_N_DAYS:7"
    q_tasks = f"SELECT WhatId, Subject, Type, Status, CreatedDate, LastModifiedDate, CompletedDateTime FROM Task WHERE WhatId IN {IL} AND (CreatedDate >= LAST_N_DAYS:7 OR LastModifiedDate >= LAST_N_DAYS:7)"
    q_events = f"SELECT WhatId, Subject, ActivityDateTime, CreatedDate FROM Event WHERE WhatId IN {IL} AND (ActivityDateTime >= LAST_N_DAYS:7 OR CreatedDate >= LAST_N_DAYS:7)"
    q_emails = f"SELECT RelatedToId, Subject, MessageDate FROM EmailMessage WHERE RelatedToId IN {IL} AND MessageDate >= LAST_N_DAYS:7"

    def run(label, q):
        res = soql(sid, instance, q)
        if res.get("_error"):
            print(f"  [{label}] ERROR: {res['_error']}")
        recs = res.get("records", [])
        print(f"  [{label}] rows returned: {len(recs)}")
        return recs

    for r in run("moves", q_moves):
        bump(r.get("OpportunityId"), r.get("CreatedDate"), "moves")
    for r in run("tasks", q_tasks):
        eff = r.get("LastModifiedDate") or r.get("CreatedDate")
        for ts in (r.get("CreatedDate"), r.get("LastModifiedDate"), r.get("CompletedDateTime")):
            if ts:
                eff = max(eff, ts)
        bump(r.get("WhatId"), eff, "tasks")
    for r in run("events", q_events):
        bump(r.get("WhatId"), r.get("ActivityDateTime") or r.get("CreatedDate"), "events")
    for r in run("emails", q_emails):
        bump(r.get("RelatedToId"), r.get("MessageDate"), "emails")

    n24 = [i for i in ids if counts[i]["24"] > 0]
    n72 = [i for i in ids if counts[i]["72"] > 0]
    n7 = [i for i in ids if counts[i]["7"] > 0]
    print(f"\nforecasted deals WITH activity/movement:  last24h={len(n24)}  last72h={len(n72)}  last7d={len(n7)}  (of {len(ids)})")

    print("\ndeals with activity in last 7d (pick demo examples):")
    for i in sorted(n7, key=lambda i: -counts[i]["7"]):
        r = by_id[i]
        d = detail[i]
        print(f"   {i} | {(r.get('account_name') or '')[:26].ljust(26)} | {str(r.get('owner_name'))[:18].ljust(18)}"
              f" | 24h={counts[i]['24']:2} 72h={counts[i]['72']:2} 7d={counts[i]['7']:2}"
              f" | tasks={d['tasks']} moves={d['moves']} events={d['events']} emails={d['emails']}")


if __name__ == "__main__":
    main()
