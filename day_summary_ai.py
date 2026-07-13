"""INTELLIGENT 24h/last-active-day summary — an LLM (Sonnet 4.5) reads the actual email
bodies + meeting notes + field changes of the deal's most recent active day and writes a
REVENUE-INTELLIGENCE briefing: what we/the buyer did and WHY, what an email was about + who
to + whether a reply is pending, what a meeting concluded (not a transcript dump), a
long-postponed meeting finally happening, etc. Natural, sharp, human — never "we sent an email".

Standalone (the user's "rerun the 24h summary separately"): pulls SF activity + Avoma notes,
calls Anthropic directly (verify=False works through Zscaler), writes ai.day_summary(source=ai).

  python day_summary_ai.py --ids 006...,006...
  python day_summary_ai.py --account "Temasek"
  python day_summary_ai.py --stage1 | --all         (--apply to write; dry-run prints)
"""
from __future__ import annotations
import sys, re, json, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import requests, urllib3
from daily_summary import common as C
from daily_summary.common import VERIFY, id15, strip_html, parse_sf
from daily_summary.extract import classify_task
from build_day_summaries import _clean_subj, _is_mase_pushed, _is_sfid, STRATEGIC, LOOKBACK
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MODEL = os.getenv("DAY_SUMMARY_MODEL", "claude-sonnet-5")

SYS = """You are a revenue-intelligence analyst briefing a deal owner on WHAT HAPPENED on the most \
recent day this deal had activity. You are given that day's raw Salesforce activity — email bodies, \
meeting/call notes, tasks, and field changes. Write like a sharp colleague giving the story, NOT a bot \
listing events.

WRITE (detailed — this is a briefing, not a telegram; detail and specificity are the goal, brevity is NOT):
- overall: a RICH 3-5 sentence narrative telling the full STORY of the day (more when the day genuinely \
warrants it). What did we or the buyer actually DO, and WHY? Who drove it (name the real people)? What \
moved, and what does it MEAN for the deal? Where does it stand now — are we waiting on them, or they on \
us, and what is pending? If a long-postponed meeting finally happened, say that. Quote the buyer where it \
sharpens the point; cite dates, dollar figures, competitor names. Insight over recitation — but never pad \
a quiet day.
- items[]: one per real activity, each a DETAILED 2-4 sentence INTELLIGENT read:
   * EMAIL — who sent it to whom, WHY (what prompted it / what it replies to), what it asked for or \
delivered, and whether a reply is now pending. Never "we sent an email" with no substance — say what it \
was about.
   * MEETING/CALL — who met, what was discussed, and what was DECIDED or CONCLUDED. Summarise the \
substance; do NOT paste the transcript.
   * MOVEMENT (field change) — what it signals (e.g. amount cut = scope/budget pullback; close pushed = slip).

RULES: Summarise, never copy-paste raw email/transcript text. No "what to do next" / recommendations \
(that lives in the to-dos). If content is thin (only a subject, no body), say what's knowable from it and \
DO NOT invent specifics or names. Ground every name in the provided text.

Return ONLY this JSON (no prose, no fences):
{"overall":"","items":[{"kind":"email|meeting|call|movement","name":"short label","summary":"the intelligent read","at":"YYYY-MM-DD"}]}"""


def _body(s, n=1800):
    t = strip_html(str(s or ""))
    t = re.sub(r"https?://\S+|Join:\s*\S*|Meeting ID:.*|Passcode:.*|Microsoft Teams meeting.*", " ", t, flags=re.I | re.S)
    t = re.sub(r"[_=*-]{4,}", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:n]


def collect_day(oid, tasks, events, emails, moves, avoma):
    """Find the last active day and gather that day's RAW content for the LLM. Returns
    (as_of_iso, list[(kind,name,at,content)]) or None."""
    ev = []
    for t in tasks:
        if _is_mase_pushed(t.get("Subject"), t.get("Description")):
            continue
        d = parse_sf(t.get("CompletedDateTime") or t.get("LastModifiedDate") or t.get("CreatedDate"))
        if not d:
            continue
        kind, _, _ = classify_task(t)
        ev.append((kind, _clean_subj(t.get("Subject")), d, _body(t.get("Description"))))
    for e in events:
        d = parse_sf(e.get("ActivityDateTime") or e.get("CreatedDate"))
        if d:
            ev.append(("meeting", _clean_subj(e.get("Subject")), d, _body(e.get("Description"))))
    for m in emails:
        d = parse_sf(m.get("MessageDate"))
        if d:
            direction = "received from buyer" if m.get("Incoming") else "sent by us"
            ev.append(("email", _clean_subj(m.get("Subject")), d, f"[{direction}] " + _body(m.get("TextBody"))))
    mv = []
    for h in moves:
        if h.get("Field") not in STRATEGIC:
            continue
        o, n = h.get("OldValue"), h.get("NewValue")
        if _is_sfid(o) and _is_sfid(n):
            continue
        d = parse_sf(h.get("CreatedDate"))
        if d:
            mv.append(("movement", f"{STRATEGIC[h['Field']]} changed", d,
                       f"{STRATEGIC[h['Field']]}: {o or '—'} -> {n or '—'}"))
    allv = ev + mv
    # FUTURE-DATED GUARD (parity with build_day_summaries): a booked FUTURE session (e.g. a
    # "CFO presentation" dated 13 Jul when today is 10 Jul) is a PLAN, not activity that
    # happened. Without this the AI narrates a scheduled meeting as delivered ("finally
    # delivered the presentation"). The 24h summary is what HAPPENED; drop future-dated items.
    import datetime as _dt
    _today = _dt.date.today()
    allv = [x for x in allv if x[2].date() <= _today]
    if not allv:
        return None
    last = max(x[2].date() for x in allv)
    day = [x for x in allv if x[2].date() == last]
    ctx = [x for x in mv if x[2].date() != last and (last - x[2].date()).days <= 21]
    # attach Avoma notes to meetings on the day (best-effort match by date)
    av_by_day = {}
    for a in (avoma or []):
        dd = str(a.get("date") or "")[:10]
        if dd:
            av_by_day.setdefault(dd, []).append(a)
    out = []
    for kind, name, d, content in sorted(day, key=lambda x: x[2], reverse=True):
        if kind in ("meeting", "call"):
            for a in av_by_day.get(last.isoformat(), []):
                if a.get("notes"):
                    content = (content + " | Avoma notes: " + _body(a["notes"], 2600)).strip(" |")
                    break
        out.append((kind, name, d.date().isoformat(), content))
    for kind, name, d, content in sorted(ctx, key=lambda x: x[2], reverse=True)[:3]:
        out.append((kind, name, d.date().isoformat(), content + " (recent context, not on the summary day)"))
    return last.isoformat(), out[:10]


def summarize(key, account, as_of, day_items):
    lines = [f"DEAL: {account}", f"MOST RECENT ACTIVE DAY: {as_of}", "", "RAW ACTIVITY:"]
    for kind, name, at, content in day_items:
        lines.append(f"\n[{kind} · {at}] {name}\n{content or '(no body captured — only the subject above)'}")
    user = "\n".join(lines)
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json={"model": MODEL, "max_tokens": 4000, "system": SYS,
                            # Sonnet 5 defaults to ADAPTIVE THINKING when `thinking` is omitted —
                            # it silently eats the token budget and truncates the JSON (the
                            # empty-overall bug). Summaries don't need thinking: disable it.
                            "thinking": {"type": "disabled"},
                            "messages": [{"role": "user", "content": user}]},
                      verify=False, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"anthropic {r.status_code}: {r.text[:120]}")
    txt = r.json().get("content", [{}])[0].get("text", "")
    m = re.search(r"\{.*\}", txt, re.S)
    obj = json.loads(m.group(0)) if m else {}
    items = obj.get("items") or []
    for it in items:
        it.setdefault("at", as_of)
    return {"as_of": as_of, "overall": str(obj.get("overall") or "").strip(),
            "items": items, "source": "ai"}


# ---------------------------------------------------------------------------
# LIVE-SWEEP ENTRY POINT — one opp, called from deal_engine_sweep so every sweep produces
# the INTELLIGENT (Sonnet, Omnivision-governed) day summary instead of the robotic template.
# Self-contained: own SF login (cached), own SOQL/Avoma read, own Anthropic call. Returns the
# summary dict (source="ai") or None on no-activity/any-failure so the caller falls back to the
# deterministic build_day_summaries backstop. NEVER raises into the sweep.
_SF = {"sec": None, "sid": None, "inst": None, "dl": None, "sys_loaded": False}


def _governed_sys(base, key):
    """Load the locked 24h-Summary (`sum`) engine into SYS once (Omnivision governance)."""
    global SYS
    if _SF["sys_loaded"]:
        return
    try:
        rows = requests.get(f"{base}/rest/v1/scoring_instructions",
                            params={"engine": "eq.sum", "locked": "eq.true",
                                    "select": "version,content", "order": "created_at.desc", "limit": "1"},
                            headers={"apikey": key, "Authorization": f"Bearer {key}"},
                            verify=VERIFY, timeout=20).json()
        if isinstance(rows, list) and rows:
            SYS = (rows[0]["content"] + "\n\n# OUTPUT ADAPTER (engine contract — unchanged): follow "
                   "the GOVERNING instruction above for WHAT to report; return ONLY the JSON below.\n\n" + SYS)
    except Exception:  # noqa: BLE001 — fail-open to the built-in prompt
        pass
    _SF["sys_loaded"] = True


def day_summary_ai_for_opp(opp_id, account=None):
    """Intelligent single-opp day summary for the live sweep. Returns dict|None (never raises)."""
    try:
        if _SF["sec"] is None:
            _SF["sec"] = C.load_secret()
            _SF["sid"], _SF["inst"] = C.sf_login(_SF["sec"])
            _SF["dl"] = C.load_datalake()
        sec = _SF["sec"]
        base = sec["SUPABASE_URL"].rstrip("/")
        key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
        ak = sec.get("ANTHROPIC_API_KEY")
        if not ak:
            return None
        _governed_sys(base, key)
        oid = id15(opp_id)
        h = {"apikey": key, "Authorization": f"Bearer {key}"}
        if not account:
            try:
                r = requests.get(f"{base}/rest/v1/deal_records",
                                 params={"select": "account_name", "opp_id": f"eq.{opp_id}"},
                                 headers=h, verify=VERIFY, timeout=20).json()
                account = (r[0].get("account_name") if r else None) or opp_id
            except Exception:  # noqa: BLE001
                account = opp_id
        IL = f"('{oid}')"
        tks = C.soql(_SF["sid"], _SF["inst"], f"SELECT WhatId,Subject,Type,Status,CreatedDate,LastModifiedDate,CompletedDateTime,Description FROM Task WHERE WhatId IN {IL} AND (CreatedDate>=LAST_N_DAYS:{LOOKBACK} OR LastModifiedDate>=LAST_N_DAYS:{LOOKBACK})")
        evs = C.soql(_SF["sid"], _SF["inst"], f"SELECT WhatId,Subject,ActivityDateTime,CreatedDate,Description FROM Event WHERE WhatId IN {IL} AND (ActivityDateTime>=LAST_N_DAYS:{LOOKBACK} OR CreatedDate>=LAST_N_DAYS:{LOOKBACK})")
        ems = C.soql(_SF["sid"], _SF["inst"], f"SELECT RelatedToId,Subject,MessageDate,Incoming,TextBody FROM EmailMessage WHERE RelatedToId IN {IL} AND MessageDate>=LAST_N_DAYS:{LOOKBACK}")
        mvs = C.soql(_SF["sid"], _SF["inst"], f"SELECT OpportunityId,Field,OldValue,NewValue,CreatedDate FROM OpportunityFieldHistory WHERE OpportunityId IN {IL} AND CreatedDate>=LAST_N_DAYS:{LOOKBACK}")
        avn = []
        if _SF["dl"]:
            av = C.datalake_get(_SF["dl"], f"avoma_meetings?crm_opportunity_id=ilike.{oid}*&select=subject,start_at,uuid&order=start_at.desc&limit=6") or []
            for m in av[:4]:
                ins = C.datalake_get(_SF["dl"], f"avoma_insights?uuid=eq.{m['uuid']}&select=ai_notes_text&limit=1")
                avn.append({"date": (m.get("start_at") or "")[:10], "notes": (ins[0].get("ai_notes_text") if ins else "") or ""})
        packet = collect_day(oid, tks, evs, ems, mvs, avn)
        if not packet:
            return None
        as_of, items = packet
        return summarize(ak, account, as_of, items)
    except Exception as e:  # noqa: BLE001 — the deterministic backstop takes over
        print(f"[DAY-SUMMARY-AI] opp={opp_id} failed: {type(e).__name__}: {str(e)[:120]}", flush=True)
        return None


def main():
    apply = "--apply" in sys.argv
    a = {}
    for i, tok in enumerate(sys.argv):
        if tok in ("--ids", "--account") and i + 1 < len(sys.argv):
            a[tok[2:]] = sys.argv[i + 1]
    sec = C.load_secret()
    base = sec["SUPABASE_URL"].rstrip("/"); key = sec.get("SUPABASE_SERVICE_ROLE_KEY") or sec.get("SUPABASE_SERVICE_KEY")
    # OMNIVISION: the locked 24-Hour-Summary instruction GOVERNS this generator. The Studio text
    # leads; the built-in SYS stays appended as the OUTPUT ADAPTER (the strict JSON contract the
    # UI renders). Fail-open to the built-in on any read error.
    global SYS
    try:
        _rows = requests.get(f"{base}/rest/v1/scoring_instructions",
                             params={"engine": "eq.sum", "locked": "eq.true",
                                     "select": "version,content", "order": "created_at.desc", "limit": "1"},
                             headers={"apikey": key, "Authorization": f"Bearer {key}"},
                             verify=VERIFY, timeout=20).json()
        if isinstance(_rows, list) and _rows:
            SYS = (_rows[0]["content"]
                   + "\n\n# OUTPUT ADAPTER (engine contract — unchanged): follow the GOVERNING "
                     "instruction above for WHAT to report; return ONLY the JSON shape below.\n\n" + SYS)
            print(f"[day-summary] governed by locked 24h-Summary v{_rows[0]['version']} (Omnivision)")
    except Exception as _e:  # noqa: BLE001
        print(f"[day-summary] studio instruction read failed ({_e}); using built-in prompt")
    ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec["SUPABASE_URL"]).group(1)
    mgmt = f"https://api.supabase.com/v1/projects/{ref}/database/query"; token = sec["SUPABASE_ACCESS_TOKEN"]
    ak = sec.get("ANTHROPIC_API_KEY"); h = {"apikey": key, "Authorization": f"Bearer {key}"}
    nm = {}
    if a.get("ids"):
        ids = [id15(x) for x in a["ids"].split(",") if x.strip()]
    elif "--stage1" in sys.argv:
        ids = [id15(x) for x in json.load(open("cc_work/_stage1.json"))]
    else:
        p = {"select": "opp_id,account_name", "active": "eq.true"}
        if a.get("account"):
            p["account_name"] = f"ilike.*{a['account']}*"
        elif "--all" not in sys.argv:
            print("pass --ids/--account/--stage1/--all"); return
        rows = requests.get(f"{base}/rest/v1/deal_records", params=p, headers=h, verify=VERIFY, timeout=150).json()
        ids = [id15(r["opp_id"]) for r in rows if r.get("opp_id")]
        for r in rows:
            nm[id15(r["opp_id"])] = r.get("account_name")
    if not nm:
        rows = requests.get(f"{base}/rest/v1/deal_records", params={"opp_id": "in.(" + ",".join(ids) + ")", "select": "opp_id,account_name"}, headers=h, verify=VERIFY, timeout=90).json()
        for r in rows:
            nm[id15(r["opp_id"])] = r.get("account_name")
    print(f"scope: {len(ids)} deals | model {MODEL}")
    dl = C.load_datalake()
    sid, inst = C.sf_login(sec)
    # gather day packets (batched SOQL)
    packets = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]; IL = "(" + ",".join("'" + x + "'" for x in chunk) + ")"
        tks = C.soql(sid, inst, f"SELECT WhatId,Subject,Type,Status,CreatedDate,LastModifiedDate,CompletedDateTime,Description FROM Task WHERE WhatId IN {IL} AND (CreatedDate>=LAST_N_DAYS:{LOOKBACK} OR LastModifiedDate>=LAST_N_DAYS:{LOOKBACK})")
        evs = C.soql(sid, inst, f"SELECT WhatId,Subject,ActivityDateTime,CreatedDate,Description FROM Event WHERE WhatId IN {IL} AND (ActivityDateTime>=LAST_N_DAYS:{LOOKBACK} OR CreatedDate>=LAST_N_DAYS:{LOOKBACK})")
        ems = C.soql(sid, inst, f"SELECT RelatedToId,Subject,MessageDate,Incoming,TextBody FROM EmailMessage WHERE RelatedToId IN {IL} AND MessageDate>=LAST_N_DAYS:{LOOKBACK}")
        mvs = C.soql(sid, inst, f"SELECT OpportunityId,Field,OldValue,NewValue,CreatedDate FROM OpportunityFieldHistory WHERE OpportunityId IN {IL} AND CreatedDate>=LAST_N_DAYS:{LOOKBACK}")
        gt, ge, gm, gv = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
        for r in tks: gt[id15(r.get("WhatId"))].append(r)
        for r in evs: ge[id15(r.get("WhatId"))].append(r)
        for r in ems: gm[id15(r.get("RelatedToId"))].append(r)
        for r in mvs: gv[id15(r.get("OpportunityId"))].append(r)
        for oid in chunk:
            av = C.datalake_get(dl, f"avoma_meetings?crm_opportunity_id=ilike.{oid}*&select=subject,start_at,uuid&order=start_at.desc&limit=6") if dl else []
            avn = []
            for m in (av or [])[:4]:
                ins = C.datalake_get(dl, f"avoma_insights?uuid=eq.{m['uuid']}&select=ai_notes_text&limit=1") if dl else []
                avn.append({"date": (m.get("start_at") or "")[:10], "notes": (ins[0].get("ai_notes_text") if ins else "") or ""})
            packets[oid] = collect_day(oid, gt.get(oid, []), ge.get(oid, []), gm.get(oid, []), gv.get(oid, []), avn)
        print(f"  gathered {min(i+50,len(ids))}/{len(ids)}", flush=True)

    todo = [(oid, p) for oid, p in packets.items() if p]
    print(f"deals with activity: {len(todo)} | summarising with {MODEL} …")
    out, errs = {}, 0

    def work(oid, packet):
        as_of, items = packet
        try:
            return oid, summarize(ak, nm.get(oid, oid), as_of, items)
        except Exception as e:
            return oid, {"_err": str(e)[:100]}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for fut in as_completed([ex.submit(work, o, p) for o, p in todo]):
            oid, res = fut.result()
            if res.get("_err"):
                errs += 1; print("  ERR", oid, res["_err"])
            else:
                out[oid] = res
    print(f"summarised {len(out)} | errors {errs}")
    for oid in list(out)[:3]:
        d = out[oid]
        print(f"\n=== {nm.get(oid)} ({d['as_of']}) ===\n  {d['overall']}")
        for it in d["items"][:3]:
            print(f"   • [{it.get('kind')}] {it.get('name')}: {it.get('summary')}")
    if not apply:
        print("\n[DRY RUN] pass --apply to write."); return
    items = list(out.items()); total = 0
    for i in range(0, len(items), 60):
        blob = json.dumps(dict(items[i:i + 60]))
        sql = ("update deal_records d set record = jsonb_set(record,'{ai,day_summary}', m.value, true), updated_at = now() "
               "from (select key as opp_id, value from jsonb_each($J$" + blob + "$J$::jsonb)) m where d.opp_id = m.opp_id returning d.opp_id")
        resp = requests.post(mgmt, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, json={"query": sql}, verify=VERIFY, timeout=120)
        if resp.status_code >= 300:
            print("APPLY FAILED", resp.status_code, resp.text[:200]); break
        total += len(resp.json())
    print(f"\nAPPLIED: {total} intelligent day_summaries written")


if __name__ == "__main__":
    main()
