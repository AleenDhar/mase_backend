"""Pure assembly + classification for the 24h window. No I/O here — callers pass in
the raw SF rows (already fetched + grouped by 15-char opp id) and get back one
structured `record` per opp, plus a deterministic natural-language summary.

Classification rules (learned from the live org):
- Clari/Outreach log emails AND meetings as Tasks and set Type="Call" on all of
  them, so the SUBJECT PREFIX ([Clari - Email], [Outreach][Email][In], [Clari -
  Meeting], Avoma - …) is authoritative; Task.Type is only a fallback.
- We window/sort by LOG time (Created/Modified/Completed) = "what entered the
  system in the last 24h". A meeting whose date is in the future was *scheduled*
  in the window (labelled upcoming), not held.
- OpportunityFieldHistory emits a readable row (names) AND a raw-id row for lookup
  fields (Owner/Co-owner); the id-only row is dropped as noise.
"""
from __future__ import annotations
import re
import datetime as dt
from .common import parse_sf, strip_html, id15

_EMAIL_MARK = ("[clari - email", "[outreach] [email", "[email]", "email received",
               "email sent", "lemlist", "e-mail", " email ")
_MEETING_MARK = ("[clari - meeting", "avoma -", "avoma:", " meeting", "demo", "workshop",
                 "kickoff", "kick-off", "qbr", "web meeting", "onsite")
_FIELD_LABEL = {
    "StageName": "Stage", "Amount": "Amount", "CloseDate": "Close Date",
    "ForecastCategoryName": "Forecast Category", "ForecastCategory": "Forecast Category",
    "Next_Step__c": "Next Step", "Type": "Type", "Probability": "Probability",
    "Opportunity_Source__c": "Opportunity Source", "OwnerId": "Owner",
    "Opportunity_Co_Owner__c": "Co-owner",
}
# A "movement" means the DEAL moved, not that a CRM record was administered. Only
# genuine deal-progression fields count: stage advance, size change, timeline
# change, forecast change. Owner/co-owner reassignment, opportunity Type,
# Probability, and Source are CRM housekeeping — they say nothing about whether the
# deal moved forward, whether the buyer needs something, or whether we did anything
# that mattered, so they are NOT movements and must never drive a 24h summary.
# (Next Step is handled on its own below as the buyer/rep next-action signal.)
# Whitelist, not blacklist, so a new admin field can never leak in as "movement".
_STRATEGIC_MOVE_FIELDS = frozenset({
    "StageName", "Amount", "CloseDate", "ForecastCategoryName", "ForecastCategory",
})
_ID_RE = re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$")


def _is_sfid(v) -> bool:
    v = ("" if v is None else str(v)).strip()
    return len(v) in (15, 18) and bool(_ID_RE.match(v)) and not v.isdigit()


def classify_task(t: dict):
    subj = (t.get("Subject") or "").lower()
    typ = (t.get("Type") or "").lower()
    sub = (t.get("TaskSubtype") or "").lower()
    status = (t.get("Status") or "").lower()
    done = status == "completed" or bool(t.get("CompletedDateTime"))
    if any(m in subj for m in _EMAIL_MARK):
        kind = "email"
    elif any(m in subj for m in _MEETING_MARK):
        kind = "meeting"
    elif "call" in typ or sub == "call":
        kind = "call"
    elif "email" in typ or sub == "email":
        kind = "email"
    elif sub == "event" or "meeting" in typ:
        kind = "meeting"
    else:
        kind = "task"
    direction = None
    if "[in]" in subj or "received" in subj:
        direction = "in"
    elif "[out]" in subj or "sent" in subj:
        direction = "out"
    return kind, done, direction


def _log_time(t: dict):
    """When the row was logged/modified — the 24h-window anchor."""
    times = [parse_sf(t.get(k)) for k in ("CompletedDateTime", "LastModifiedDate", "CreatedDate")]
    times = [x for x in times if x]
    return max(times) if times else None


def _event_dt(val):
    """A meeting/activity date that may be date-only ('2026-07-06') or a datetime."""
    if not val:
        return None
    v = val if "T" in val else (val + "T00:00:00Z")
    return parse_sf(v)


def assemble(meta: dict, opp_fields: dict, tasks: list, events: list, emails: list,
             moves: list, avoma: list, window_start: dt.datetime, now: dt.datetime) -> dict:
    raw = []

    def add(kind, subject, at, when, *, done, direction=None, owner=None,
            type_=None, source="task"):
        raw.append({
            "kind": kind, "subject": subject, "type": type_, "done": done,
            "direction": direction, "owner": owner, "at": at.isoformat(),
            "when": when.isoformat() if when else None,
            "upcoming": bool(when and when > now), "source": source,
        })

    for t in tasks:
        kind, done, direction = classify_task(t)
        at = _log_time(t)  # when the rep logged it — the 24h anchor
        if not at or at < window_start:
            continue
        when = _event_dt(t.get("ActivityDate")) if kind == "meeting" else None
        add(kind, t.get("Subject"), at, when, done=done, direction=direction,
            owner=((t.get("Owner") or {}) or {}).get("Name"), type_=t.get("Type"), source="task")

    for e in events:
        logged = parse_sf(e.get("CreatedDate"))
        when = parse_sf(e.get("ActivityDateTime"))
        held_recent = bool(when and window_start <= when <= now)
        logged_recent = bool(logged and logged >= window_start)
        if not (held_recent or logged_recent):
            continue  # a future meeting scheduled long ago is NOT last-24h activity
        anchor = when if held_recent else logged
        add("meeting", e.get("Subject"), anchor, when, done=bool(when and when <= now),
            owner=((e.get("Owner") or {}) or {}).get("Name"), type_="Event", source="event")

    for m in emails:
        at = parse_sf(m.get("MessageDate"))
        if not at or at < window_start:
            continue
        add("email", m.get("Subject"), at, None, done=True,
            direction="in" if m.get("Incoming") else "out",
            owner=m.get("FromAddress"), type_="EmailMessage", source="emailmessage")

    # de-dupe: the same session/email is often logged twice (Clari + Avoma/Outreach)
    seen, activities = set(), []
    for a in sorted(raw, key=lambda x: x["at"], reverse=True):
        key = (a["kind"], (a["subject"] or "").strip().lower()[:90], (a["at"] or "")[:13])
        if key in seen:
            continue
        seen.add(key)
        activities.append(a)

    counts = {"calls": 0, "emails": 0, "meetings": 0, "meetings_scheduled": 0,
              "tasks": 0, "movements": 0, "next_step_changed": 0, "total": 0}
    for a in activities:
        if a["kind"] == "call":
            counts["calls"] += 1
        elif a["kind"] == "email":
            counts["emails"] += 1
        elif a["kind"] == "meeting":
            counts["meetings_scheduled" if a["upcoming"] else "meetings"] += 1
        else:
            counts["tasks"] += 1

    movements = []
    next_step_changed_at = None
    for h in moves:
        at = parse_sf(h.get("CreatedDate"))
        if not at or at < window_start:
            continue
        field = h.get("Field")
        if field in ("Next_Step__c", "Next_Step_History__c"):
            next_step_changed_at = at.isoformat()
            counts["next_step_changed"] = 1
            continue
        if field not in _STRATEGIC_MOVE_FIELDS:
            continue  # CRM housekeeping (Owner/Co-owner/Type/Probability/Source): not a deal movement
        old, new = h.get("OldValue"), h.get("NewValue")
        if _is_sfid(old) and _is_sfid(new):
            continue  # raw lookup-id row; the readable (names) row carries the change
        movements.append({
            "field": field, "label": _FIELD_LABEL.get(field, field),
            "old": old, "new": new, "at": at.isoformat(),
            "by": ((h.get("CreatedBy") or {}) or {}).get("Name"),
        })
        counts["movements"] += 1

    ns_ts = parse_sf(opp_fields.get("Next_Step_Updated_Date_Time__c"))
    if ns_ts and ns_ts >= window_start:
        next_step_changed_at = next_step_changed_at or ns_ts.isoformat()
        counts["next_step_changed"] = 1

    meetings_avoma = [{
        "subject": a.get("subject"), "start_at": a.get("start_at"),
        "is_call": a.get("is_call"), "transcript_ready": a.get("transcript_ready"),
        "notes_ready": a.get("notes_ready"),
    } for a in (avoma or [])]

    activities.sort(key=lambda x: x["at"], reverse=True)
    movements.sort(key=lambda x: x["at"], reverse=True)
    counts["total"] = len(activities) + len(movements) + counts["next_step_changed"]
    has_activity = counts["total"] > 0 or bool(meetings_avoma)

    return {
        "opp_id": id15(meta.get("opp_id")),
        "account_name": meta.get("account_name"),
        "opp_name": meta.get("opp_name"),
        "owner_name": meta.get("owner_name"),
        "forecast_category": meta.get("forecast_category"),
        "stage": opp_fields.get("StageName") or meta.get("stage"),
        "amount": opp_fields.get("Amount"),
        "close_date": opp_fields.get("CloseDate"),
        "next_step_text": strip_html(opp_fields.get("Next_Step__c")),
        "next_step_changed_at": next_step_changed_at,
        "window_start": window_start.isoformat(),
        "window_end": now.isoformat(),
        "counts": counts,
        "movements": movements,
        "activities": activities,
        "meetings_avoma": meetings_avoma,
        "has_activity": has_activity,
    }


# --- deterministic narrative (free, scalable fallback) -----------------------
def _fmt(iso: str) -> str:
    d = parse_sf(iso)
    return d.strftime("%b %d %H:%M") if d else ""


NO_ACTIVITY_TEXT = "No moment registered in last twenty four hours in this ticket"


def _clean_subj(s) -> str:
    """Strip logging-tool prefixes so a subject reads like plain English
    ('[Clari - Email Sent] Re: Checking In' -> 'Re: Checking In')."""
    t = re.sub(r"^(\s*\[[^\]]*\]\s*)+", "", str(s or "").strip())
    t = re.sub(r"^(avoma|clari|gong|outreach|lemlist)\s*[-:–]\s*", "", t, flags=re.I)
    return t.strip()


def _act_verb(a: dict) -> str:
    k, d = a.get("kind"), a.get("direction")
    if k == "email":
        return "sent an email" if d == "out" else ("received an email" if d == "in" else "emailed")
    if k == "call":
        return "had a call"
    if k == "meeting":
        return "scheduled a meeting" if a.get("upcoming") else "met"
    return "logged a task"


def deterministic_summary(rec: dict) -> str:
    """A plain-English PROSE summary of the day's activity — never a metadata dump.
    Names who did what, on which item (cleaned of [Clari - …] prefixes), and when."""
    if not rec.get("has_activity"):
        return NO_ACTIVITY_TEXT
    owner = rec.get("owner_name") or "The team"
    sentences: list[str] = []
    # real deal progression first
    for mv in (rec.get("movements") or []):
        sentences.append(f"{mv.get('label')} moved {(mv.get('old') or '—')} → {(mv.get('new') or '—')}"
                         + (f" (by {mv['by']})" if mv.get("by") else "") + ".")
    # activities as prose — up to 3 named, the rest counted
    acts = rec.get("activities") or []
    named = []
    for a in acts[:3]:
        who = str(a.get("owner") or owner).split()[0] if (a.get("owner") or owner) else owner
        subj = _clean_subj(a.get("subject"))
        named.append(f"{who} {_act_verb(a)}" + (f" — {subj}" if subj else "") + f" ({_fmt(a['at'])})")
    if named:
        sentences.append("; ".join(named) + ".")
    extra = len(acts) - 3
    if extra > 0:
        sentences.append(f"Plus {extra} more activit{'y' if extra == 1 else 'ies'} logged.")
    avoma = [_clean_subj(m.get("subject")) for m in (rec.get("meetings_avoma") or []) if m.get("subject")][:2]
    if avoma:
        sentences.append("Avoma call" + ("s" if len(avoma) > 1 else "") + ": " + "; ".join(avoma) + ".")
    if rec.get("next_step_changed_at"):
        sentences.append("Next step was updated.")
    return " ".join(sentences).strip()
