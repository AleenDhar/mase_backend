"""READ-ONLY accuracy check for one opp: compare what the drawer shows (from the
stored deal_records sweep) against LIVE Salesforce. Writes nothing."""
import json, datetime as dt
from daily_summary.common import load_secret, sf_login, soql, sb_get, id15, parse_sf

OID = "006P700000J71MD"  # Austrian Post


def main():
    sec = load_secret()
    now = dt.datetime.now(dt.timezone.utc)
    print("now(UTC):", now.strftime("%Y-%m-%d %H:%M"))

    # ---- stored deal_records (what the drawer reads) -------------------------
    code, rows = sb_get(sec, f"deal_records?opp_id=like.{OID}*&select=opp_id,account_name,opp_name,forecast_category,stage,amount,close_date,swept_at,updated_at,last_activity_date,record")
    rec = rows[0] if isinstance(rows, list) and rows else {}
    R = rec.get("record") or {}
    hard = R.get("hard") or {}
    ai = R.get("ai") or {}
    print("\n===== STORED (deal_records / sweep) =====")
    print(" flat.forecast_category :", rec.get("forecast_category"))
    print(" flat.stage            :", rec.get("stage"))
    print(" flat.last_activity_date:", rec.get("last_activity_date"))
    print(" swept_at              :", rec.get("swept_at"), " updated_at:", rec.get("updated_at"))
    print(" hard.opp_name         :", hard.get("opp_name"))
    print(" hard.forecast_category:", hard.get("forecast_category"))
    print(" hard.stage            :", hard.get("stage"))
    print(" hard.amount           :", hard.get("amount"))
    print(" hard.close_date       :", hard.get("close_date"))
    print(" hard.last_activity_date:", hard.get("last_activity_date"))
    ci = ai.get("ceo_intervention")
    if ci:
        print(" ai.ceo_intervention.needed  :", ci.get("needed"), "| priority:", ci.get("priority"))
        print(" ai.ceo_intervention.reason  :", (ci.get("reason") or "")[:200])
    sw = parse_sf(rec.get("swept_at"))
    if sw:
        if sw.tzinfo is None:
            sw = sw.replace(tzinfo=dt.timezone.utc)
        print(f" >> sweep age: {(now - sw).days} days old (swept {sw.date()})")

    # ---- LIVE Salesforce ----------------------------------------------------
    sid, inst = sf_login(sec)
    opp = soql(sid, inst, f"SELECT Id, Name, StageName, Amount, CloseDate, ForecastCategoryName, LastActivityDate, Next_Step__c, Next_Step_Updated_Date_Time__c, Owner.Name FROM Opportunity WHERE Id='{OID}'")
    o = opp[0] if opp else {}
    print("\n===== LIVE SALESFORCE =====")
    print(" Name                :", o.get("Name"))
    print(" StageName           :", o.get("StageName"))
    print(" Amount              :", o.get("Amount"))
    print(" CloseDate           :", o.get("CloseDate"))
    print(" ForecastCategoryName:", o.get("ForecastCategoryName"))
    print(" LastActivityDate    :", o.get("LastActivityDate"))
    ns = (o.get("Next_Step__c") or "").replace("\n", " ")
    print(" Next_Step__c        :", ns[:160])
    print(" Next_Step_Updated   :", o.get("Next_Step_Updated_Date_Time__c"))

    # true most-recent activity (Tasks + Events), last 60d
    tasks = soql(sid, inst, f"SELECT Subject, ActivityDate, CreatedDate, Type FROM Task WHERE WhatId='{OID}' AND CreatedDate >= LAST_N_DAYS:60 ORDER BY CreatedDate DESC LIMIT 8")
    events = soql(sid, inst, f"SELECT Subject, ActivityDateTime, CreatedDate FROM Event WHERE WhatId='{OID}' AND (ActivityDateTime >= LAST_N_DAYS:60 OR CreatedDate >= LAST_N_DAYS:60) ORDER BY CreatedDate DESC LIMIT 8")
    print(f"\n recent Tasks (60d): {len(tasks)}")
    for t in tasks[:6]:
        print("   ", t.get("CreatedDate", "")[:10], "|", (t.get("Subject") or "")[:70])
    print(f" recent Events (60d): {len(events)}")
    for e in events[:6]:
        print("   ", (e.get("ActivityDateTime") or e.get("CreatedDate") or "")[:16], "|", (e.get("Subject") or "")[:70])
    # compute true last activity date
    alldts = [parse_sf(t.get("CreatedDate")) for t in tasks] + \
             [parse_sf(e.get("ActivityDateTime") or e.get("CreatedDate")) for e in events]
    alldts = [d for d in alldts if d]
    if alldts:
        last = max(alldts)
        print(f" >> TRUE most-recent SF activity: {last.date()}  ({(now - last).days}d ago)")

    # contacts / roles — verify "CFO Flandorfer never engaged"
    roles = soql(sid, inst, f"SELECT Contact.Name, Contact.Title, Role, IsPrimary FROM OpportunityContactRole WHERE OpportunityId='{OID}'")
    print(f"\n OpportunityContactRoles: {len(roles)}")
    for r in roles:
        c = r.get("Contact") or {}
        print("   ", (c.get("Name") or "?"), "|", (c.get("Title") or ""), "| role:", r.get("Role"), "| primary:", r.get("IsPrimary"))
    hit = [r for r in roles if "flandorfer" in ((r.get("Contact") or {}).get("Name") or "").lower()]
    print(" 'Flandorfer' in contact roles:", "YES" if hit else "NO")


if __name__ == "__main__":
    main()
