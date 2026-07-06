"""Standalone 24h/last-active-day summary builder — generates ai.day_summary for a deal
DIRECTLY from its Salesforce activity, independent of the (flaky) LLM sweep. This is the
reliable backbone the deal drawer's "24h Summary" tab reads.

Design (per the user's spec):
  * Show the LAST DAY THAT HAS ACTIVITY (not a fixed 24h window). If nothing happened
    today, we still show the most recent real day, with its date.
  * Summarise, don't dump: each meeting/call/email/movement is NAMED (cleaned of
    [Clari - ...] prefixes) with a one-line what-happened; plus an overall narrative.
    NEVER a raw email/transcript dump; NEVER "what to do next".
  * Strategic field-moves (Stage/Amount/CloseDate/Forecast) count as activity — an
    amount cut or a close-date push IS a movement worth surfacing.

Reads SF via the local Zscaler-friendly helpers; writes ai.day_summary via opp-scoped
jsonb_set (Supabase Management API). Batched SOQL so it scales to the whole book.

Usage:
  python build_day_summaries.py --ids 006...,006...        # specific opps
  python build_day_summaries.py --account "Austrian Post"  # by name
  python build_day_summaries.py --all                      # every active deal
  (dry-run prints the summaries; add --apply to write)
"""
from __future__ import annotations
import sys, re, json, datetime as dt
from collections import defaultdict
import requests, urllib3
from daily_summary import common as C
from daily_summary.common import VERIFY, id15, strip_html, parse_sf
from daily_summary.extract import classify_task
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LOOKBACK = 120
STRATEGIC = {"StageName": "Stage", "Amount": "Amount", "CloseDate": "Close date",
             "ForecastCategoryName": "Forecast", "ForecastCategory": "Forecast"}
_ID_RE = re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$")


def _clean_subj(s) -> str:
    t = re.sub(r"^(\s*\[[^\]]*\]\s*)+", "", str(s or "").strip())
    t = re.sub(r"^(avoma|clari|gong|outreach|lemlist)\s*[-:–]\s*", "", t, flags=re.I)
    return t.strip(" -:–") or "(untitled)"


def _snippet(s, n=140) -> str:
    t = strip_html(str(s or ""))
    # strip logging-tool boilerplate so we keep only the substance (never a raw dump)
    t = re.sub(r"--\s*Avoma[^-]*?(?:Start|End)\s*--", " ", t, flags=re.I)   # -- Avoma Note Start --
    t = re.sub(r"Avoma\s*-\s*:[^\n]*?(?:<>[^\n]*\d{4}|\n|$)", " ", t)        # "Avoma - : Title <> July 01, 2026"
    t = re.sub(r"[_]{4,}|[-]{4,}|[=]{4,}|\*{3,}", " ", t)                    # rule lines
    t = re.sub(r"Microsoft Teams meeting.*", " ", t, flags=re.I | re.S)      # Teams join blurb
    t = re.sub(r"https?://\S+|Join:\s*\S*|Meeting ID:.*|Passcode:.*", " ", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^(hi|hello|hey|dear)\b[^,.:]{0,30}[,:]\s*", "", t, flags=re.I)  # leading greeting
    # cut at a quoted-reply / signature boundary so we don't drag in the thread history
    t = re.split(r"(?:^|\s)(?:On .{0,50}wrote:|From:\s|Sent:\s|-----Original|Best regards|Kind regards|Mit freundlichen|Thanks,|Regards,|Von:\s)", t)[0].strip()
    return (t[:n].rstrip() + "…") if len(t) > n else t


def _is_sfid(v) -> bool:
    v = ("" if v is None else str(v)).strip()
    return len(v) in (15, 18) and bool(_ID_RE.match(v)) and not v.isdigit()


# MASE writes its OWN to-dos back to Salesforce (the "push to SFDC" feature). Those Tasks are
# NOT real buyer/deal activity — they're our own notes echoed back. Exclude them so the 24h
# summary shows what the BUYER/deal did, not what MASE logged.
_MASE_NOISE = re.compile(r"deal engine to-?do|pushed from espresso|logged from mase|"
                         r"\bmase\b.*to-?do|best-?practice flag|grounding quote:", re.I)


def _is_mase_pushed(subject, desc) -> bool:
    blob = f"{subject or ''} {desc or ''}"
    return bool(_MASE_NOISE.search(blob))


def _kind_verb(kind, direction):
    if kind == "email":
        return "Email sent" if direction == "out" else ("Email received" if direction == "in" else "Email")
    if kind == "call":
        return "Call"
    if kind == "meeting":
        return "Meeting"
    return "Task"


def build_one(oid, tasks, events, emails, moves):
    """Return an ai.day_summary dict, or None when there is genuinely no activity at all."""
    items = []  # (date, kind, name, summary)

    def when(*cands):
        for c in cands:
            d = parse_sf(c)
            if d:
                return d
        return None

    for t in tasks:
        if _is_mase_pushed(t.get("Subject"), t.get("Description")):
            continue   # our own to-do echoed back to SF — not real activity
        kind, done, direction = classify_task(t)
        d = when(t.get("CompletedDateTime"), t.get("LastModifiedDate"), t.get("CreatedDate"))
        if not d:
            continue
        name = _clean_subj(t.get("Subject"))
        desc = _snippet(t.get("Description"))
        summ = desc or f"{_kind_verb(kind, direction)} logged."
        items.append((d, kind, name, summ))
    for e in events:
        d = when(e.get("ActivityDateTime"), e.get("CreatedDate"))
        if not d:
            continue
        items.append((d, "meeting", _clean_subj(e.get("Subject")),
                      _snippet(e.get("Description")) or "Meeting held."))
    for m in emails:
        d = when(m.get("MessageDate"))
        if not d:
            continue
        direction = "in" if m.get("Incoming") else "out"
        body = _snippet(m.get("TextBody"))
        items.append((d, "email", _clean_subj(m.get("Subject")),
                      body or f"{_kind_verb('email', direction)}."))

    move_items = []
    for h in moves:
        f = h.get("Field")
        if f not in STRATEGIC:
            continue
        old, new = h.get("OldValue"), h.get("NewValue")
        if _is_sfid(old) and _is_sfid(new):
            continue
        d = parse_sf(h.get("CreatedDate"))
        if not d:
            continue
        lbl = STRATEGIC[f]
        move_items.append((d, "movement", f"{lbl} changed",
                           f"{lbl} moved {old if old not in (None,'') else '—'} → {new if new not in (None,'') else '—'}."))

    allev = items + move_items
    if not allev:
        return None
    # DE-DUP: the same session/email is often logged 2-3x (Avoma note + event description +
    # Clari copy). Collapse by (kind, name-key, day); keep the richest summary.
    seen = {}
    for e in allev:
        d, kind, name, summ = e
        k = (kind, re.sub(r"\W+", "", name.lower())[:36], d.date())
        if k not in seen or len(summ or "") > len(seen[k][3] or ""):
            seen[k] = e
    allev = list(seen.values())
    items = [e for e in allev if e[1] != "movement"]
    move_items = [e for e in allev if e[1] == "movement"]
    # LAST DAY WITH ACTIVITY: the most recent calendar day that carries a real event.
    last_day = max(e[0].date() for e in allev)
    day_items = [e for e in allev if e[0].date() == last_day]
    # recent strategic movements (last 21d) as context even if on other days
    ctx_moves = [e for e in move_items if e[0].date() != last_day
                 and (last_day - e[0].date()).days <= 21]

    def to_item(e):
        d, kind, name, summ = e
        return {"kind": kind, "name": name, "summary": summ, "at": d.date().isoformat()}

    out_items = [to_item(e) for e in sorted(day_items, key=lambda x: x[0], reverse=True)]
    out_items += [to_item(e) for e in sorted(ctx_moves, key=lambda x: x[0], reverse=True)]
    out_items = out_items[:8]

    # OVERALL narrative
    n_day = len(day_items)
    kinds = defaultdict(int)
    for _, k, _, _ in day_items:
        kinds[k] += 1
    bits = []
    for k, lbl in (("meeting", "meeting"), ("call", "call"), ("email", "email"),
                   ("movement", "deal change"), ("task", "task")):
        if kinds.get(k):
            bits.append(f"{kinds[k]} {lbl}{'s' if kinds[k] != 1 else ''}")
    day_str = f"{last_day.day} {last_day.strftime('%b')}"
    lead_name = day_items[0][2] if day_items else ""
    overall = (f"Most recent activity was on {day_str}: " + (", ".join(bits) if bits else "activity")
               + (f" — {lead_name}." if lead_name else "."))
    if ctx_moves:
        cm = "; ".join(x[3].rstrip(".") for x in sorted(ctx_moves, key=lambda x: x[0], reverse=True)[:3])
        overall += f" Recent deal changes: {cm}."
    return {"as_of": last_day.isoformat(), "overall": overall, "items": out_items, "source": "sf_activity"}


# Cached SF session so the sweep can call day_summary_for_opp() per-opp without a login each time.
_SF = {"sid": None, "inst": None}


def day_summary_for_opp(opp_id, sid=None, inst=None):
    """Build ai.day_summary for ONE opp from live Salesforce activity. For the SWEEP to call
    on every run so the 24h summary refreshes with the rest of the record. Returns the dict or
    None (no activity). Reuses a cached SF session unless (sid, inst) are passed. Never raises
    is the CALLER's job — wrap this in try/except so a summary hiccup can't fail a sweep."""
    oid = id15(opp_id)
    if sid is None or inst is None:
        if not _SF["sid"]:
            _SF["sid"], _SF["inst"] = C.sf_login(C.load_secret())
        sid, inst = _SF["sid"], _SF["inst"]
    IL = "('" + oid + "')"
    tasks = C.soql(sid, inst, f"SELECT WhatId,Subject,Type,Status,CreatedDate,LastModifiedDate,CompletedDateTime,Description FROM Task WHERE WhatId IN {IL} AND (CreatedDate>=LAST_N_DAYS:{LOOKBACK} OR LastModifiedDate>=LAST_N_DAYS:{LOOKBACK})")
    events = C.soql(sid, inst, f"SELECT WhatId,Subject,ActivityDateTime,CreatedDate,Description FROM Event WHERE WhatId IN {IL} AND (ActivityDateTime>=LAST_N_DAYS:{LOOKBACK} OR CreatedDate>=LAST_N_DAYS:{LOOKBACK})")
    emails = C.soql(sid, inst, f"SELECT RelatedToId,Subject,MessageDate,Incoming,TextBody FROM EmailMessage WHERE RelatedToId IN {IL} AND MessageDate>=LAST_N_DAYS:{LOOKBACK}")
    moves = C.soql(sid, inst, f"SELECT OpportunityId,Field,OldValue,NewValue,CreatedDate FROM OpportunityFieldHistory WHERE OpportunityId IN {IL} AND CreatedDate>=LAST_N_DAYS:{LOOKBACK}")
    return build_one(oid, tasks, events, emails, moves)


def main():
    apply = "--apply" in sys.argv
    a = {}
    for i, tok in enumerate(sys.argv):
        if tok in ("--ids", "--account") and i + 1 < len(sys.argv):
            a[tok[2:]] = sys.argv[i + 1]
    sec = C.load_secret()
    base = sec["SUPABASE_URL"].rstrip("/")
    key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
    mgmt = f"https://api.supabase.com/v1/projects/{ref}/database/query"
    token = sec["SUPABASE_ACCESS_TOKEN"]
    h = {"apikey": key, "Authorization": f"Bearer {key}"}

    # resolve scope
    params = {"select": "opp_id,account_name", "active": "eq.true"}
    if a.get("ids"):
        ids = [id15(x) for x in a["ids"].split(",") if x.strip()]
    else:
        if a.get("account"):
            params["account_name"] = f"ilike.*{a['account']}*"
        elif "--all" not in sys.argv:
            print("pass --ids, --account, or --all"); return
        rows = requests.get(f"{base}/rest/v1/deal_records", params=params, headers=h, verify=VERIFY, timeout=150).json()
        ids = [id15(r["opp_id"]) for r in rows if r.get("opp_id")]
    print(f"scope: {len(ids)} opps")
    if not ids:
        return

    sid, inst = C.sf_login(sec)
    out, built, empty = {}, 0, 0
    # batch SOQL in chunks of ~60 ids
    for i in range(0, len(ids), 60):
        chunk = ids[i:i + 60]
        IL = "(" + ",".join("'" + x + "'" for x in chunk) + ")"
        tasks = C.soql(sid, inst, f"SELECT WhatId,Subject,Type,Status,CreatedDate,LastModifiedDate,CompletedDateTime,Description FROM Task WHERE WhatId IN {IL} AND (CreatedDate>=LAST_N_DAYS:{LOOKBACK} OR LastModifiedDate>=LAST_N_DAYS:{LOOKBACK})")
        events = C.soql(sid, inst, f"SELECT WhatId,Subject,ActivityDateTime,CreatedDate,Description FROM Event WHERE WhatId IN {IL} AND (ActivityDateTime>=LAST_N_DAYS:{LOOKBACK} OR CreatedDate>=LAST_N_DAYS:{LOOKBACK})")
        emails = C.soql(sid, inst, f"SELECT RelatedToId,Subject,MessageDate,Incoming,TextBody FROM EmailMessage WHERE RelatedToId IN {IL} AND MessageDate>=LAST_N_DAYS:{LOOKBACK}")
        moves = C.soql(sid, inst, f"SELECT OpportunityId,Field,OldValue,NewValue,CreatedDate FROM OpportunityFieldHistory WHERE OpportunityId IN {IL} AND CreatedDate>=LAST_N_DAYS:{LOOKBACK}")
        g = lambda rows, k: (lambda d: [d[id15(r.get(k))].append(r) for r in rows] and d or d)(defaultdict(list))
        gt, ge, gm, gem = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
        for r in tasks: gt[id15(r.get("WhatId"))].append(r)
        for r in events: ge[id15(r.get("WhatId"))].append(r)
        for r in emails: gem[id15(r.get("RelatedToId"))].append(r)
        for r in moves: gm[id15(r.get("OpportunityId"))].append(r)
        for oid in chunk:
            ds = build_one(oid, gt.get(oid, []), ge.get(oid, []), gem.get(oid, []), gm.get(oid, []))
            if ds:
                out[oid] = ds; built += 1
            else:
                empty += 1
        print(f"  processed {min(i+60,len(ids))}/{len(ids)} | built {built} | no-activity {empty}", flush=True)

    # show samples
    for oid in list(out)[:4]:
        d = out[oid]
        print(f"\n=== {oid} (as_of {d['as_of']}) ===\n  {d['overall'][:200]}")
        for it in d["items"][:4]:
            print(f"   • [{it['kind']}] {it['name'][:40]} — {it['summary'][:70]} ({it['at']})")
    if not apply:
        print(f"\n[DRY RUN] built {built}, no-activity {empty}. pass --apply to write.")
        return
    total = 0
    items = list(out.items())
    for i in range(0, len(items), 60):
        blob = json.dumps(dict(items[i:i + 60]))
        sql = ("update deal_records d set record = jsonb_set(record,'{ai,day_summary}', m.value, true), "
               "updated_at = now() from (select key as opp_id, value from jsonb_each($J$" + blob + "$J$::jsonb)) m "
               "where d.opp_id = m.opp_id returning d.opp_id")
        resp = requests.post(mgmt, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                             json={"query": sql}, verify=VERIFY, timeout=120)
        if resp.status_code >= 300:
            print("APPLY FAILED", resp.status_code, resp.text[:300]); break
        total += len(resp.json())
    print(f"\nAPPLIED: {total} day_summaries written to ai.day_summary")


if __name__ == "__main__":
    main()
