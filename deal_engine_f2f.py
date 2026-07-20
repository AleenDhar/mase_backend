"""Face-to-face EXECUTIVE CONNECT detection — deterministic, no LLM.

Answers one question per deal: has a physically in-person meeting happened with a
senior BUYER-side person present?  Emits {"status": "done"|"planned"|"none", ...}.

WHY THIS IS INFERENCE, NOT A LOOKUP.  Salesforce HAS the purpose-built field —
`Event.Location_Medium__c`, picklist ['Webex/Video Conference','N/A','Onsite'] — and it is
100% NULL org-wide (0 of 17,943 opportunity Events in 180 days).  `Meeting_Sub_Type__c`
is likewise 0% populated.  `Event.Location` is ~3% populated and is usually the literal
string "Microsoft Teams Meeting", so it can VETO an in-person claim but can never
establish one.  Everything below is therefore evidence-weighing over free text, and it
is deliberately CONSERVATIVE: we would rather say "none" than assert a meeting happened.

MEASURED FAILURE MODES this module exists to prevent (all real, all from the book):
  - Allstate: "Zycus team is available to visit the Chicago office" — an OFFER, sitting
    inside a Zoom invite.  Naive keyword matching scored it as a completed onsite.
  - Manscaped: claimed F2F on a date with no Event at all; the AE writes in the same
    thread "I did not get a chance to speak with you at the conference".
  - Goldman Sachs: "Onsite Meet Up" is a Task with Status='Planned'.
  - Capitec: an "In-Person" meeting whose buyer attendees are room-booking mailboxes.
  - Beko: a genuine street-address onsite, but every attendee is a Senior Specialist.
  - CLARINS: real onsite, CPO absent — the rep is still chasing a FIRST CPO meeting.
Hand-tuned against 52 forecasted deals, this gate still got 2 of 6 "done" verdicts wrong
before adversarial review.  Treat `status="done"` as "there is citable evidence", never
as ground truth — which is why `evidence` is mandatory and surfaced in the UI.

STRUCTURAL LIMIT: EventRelation records buyer Contacts ONLY — zero Zycus-side Users.
We can prove THEIR executive was in the room; we can never prove ours was.  So this is
"an executive was present", not "senior-to-senior".  Do not let the UI imply otherwise.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

STATUS_DONE = "done"
STATUS_PLANNED = "planned"
STATUS_NONE = "none"

# ── in-person markers ────────────────────────────────────────────────────────────
# Deliberately excludes bare "visit" (matches "revisit"/"website visit"/"visit our
# docs") and bare "conference" (matches "conference call" — the single most common
# VIRTUAL meeting subject in the org).  Both were measured false-positive generators.
_IN_PERSON = (
    "onsite", "on-site", "on site", "face to face", "face-to-face", "f2f",
    "in person", "in-person", "office visit", "site visit", "hq visit",
    "lunch meeting", "dinner", "breakfast meeting", "booth", "physically present",
    "hosted us", "meet up", "meetup", "walk in", "walk-in",
)
# "conference" only counts when it is clearly an EVENT you travel to, never
# "conference call" / "conference bridge" / "audio conference".
_CONFERENCE_OK = ("conference room", "at the conference", "conference booth",
                  "summit", "trade show", "expo")
_CONFERENCE_BAD = ("conference call", "conference bridge", "audio conference",
                   "conf call", "video conference", "web conference")

# ── virtual-join tells (veto an in-person claim) ─────────────────────────────────
_VIRTUAL = (
    "zoom.us", "teams.microsoft.com", "teams.live.com", "webex.com", "webex",
    "meet.google", "gotomeeting", "gotomeet.me", "bluejeans", "chime.aws",
    "ringcentral", "whereby.com", "join the meeting", "join zoom meeting",
    "microsoft teams meeting", "dial-in", "dial in number", "conference id:",
    "meeting id:", "passcode:", "join by phone",
)
# ...unless the text ALSO asserts physical presence.  Hybrid meetings are real:
# Tata's "all Tata participants requested to be physically present in one room"
# arrived inside an invite that also carried a Teams link.
_PHYSICAL_OVERRIDE = (
    "physically present", "in one room", "at our office", "at your office",
    "in the office", "please come to", "venue:", "ort:", "address:",
    "kindly assemble", "in-person attendance", "attend in person",
)

# ── aspiration (a plan, not a record of something that happened) ─────────────────
_ASPIRATIONAL = (
    "available to visit", "would like to meet", "would love to meet",
    "looking forward to your confirmation", "if travel permits", "aim to meet",
    "hoping to meet", "hope to meet", "plan to meet", "planning a", "planning to",
    "try for an onsite", "propose to meet", "proposition of a meeting",
    "shall we meet", "can we meet", "let us meet", "keen to meet",
    "to be scheduled", "tbd", "yet to meet", "on standby", "when next in",
    "will set up", "to set up", "need to set up", "requesting a meeting",
    "invite", "invitation", "rsvp",
)
_FUTURE_INTENT = re.compile(
    r"\b(will|shall|going to|to be|scheduled for|booked for|planning|upcoming|next week|next month)\b",
    re.I,
)
_PAST_TENSE = re.compile(
    r"\b(met|meeting went|had a|hosted|visited|was on ?site|attended|caught up|"
    r"we were|they were|went very well|good meeting|productive)\b",
    re.I,
)

# ── executive seniority ─────────────────────────────────────────────────────────
# Ordered: the first pattern that matches wins, so junior traps are tested FIRST.
_NOT_EXEC = (
    "executive assistant", "assistant to", "tender executive", "sales executive",
    "account executive", "marketing executive", "hr executive", "admin executive",
    "procurement executive", "executive secretary", "personal assistant",
    "senior specialist", "specialist", "analyst", "engineer", "consultant",
    "coordinator", "administrator", "officer",  # "officer" unless Chief — see below
    "associate", "intern", "trainee", "supervisor", "team lead", "teamlead",
)
_EXEC = (
    "chief", "ceo", "cfo", "cpo", "cio", "cto", "cdo", "coo", "cmo", "cso",
    "president", "managing director", "manging director", "md,", "vice president",
    "vp,", "svp", "evp", "avp", "group director", "deputy director", "director",
    "head of", "head,", "head -", "global head", "group head", "regional head",
    "general manager", "senior general manager", "senior gm", " gm,", "partner",
    "country manager", "owner", "founder", "board member", "treasurer",
    "controller", "principal",
)
# Titles that literally start with "head" but no comma/of — "Head Procurement",
# "Head Supply Chain".  Real and senior; a naive `head of` regex misses them.
_HEAD_PREFIX = re.compile(r"^\s*(global |group |regional |deputy )?head\b", re.I)
# "Officer" is junior UNLESS it is a chief-officer title.
_CHIEF_OFFICER = re.compile(r"\bchief\b.*\bofficer\b", re.I)

# ── non-people that masquerade as Contacts ──────────────────────────────────────
_ROOM_WORDS = ("room", "boardroom", "meeting room", "conf room", "conference room",
               "auditorium", "cafeteria", "reception", "resource", "projector",
               "vc unit", "video unit", "training centre", "training center")
_ADDRESSY = re.compile(
    r"\b(floor|fl\b|street|st\.|road|rd\.|avenue|ave\b|building|bldg|campus|house|"
    r"tower|block|suite|plaza|park\b|level \d|\d{3,})\b", re.I)


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


# Word-boundary matching is mandatory, not cosmetic: plain substring search makes
# "website visit report" match "site visit", and "revisit" match "visit".
_IN_PERSON_RE = re.compile(
    r"(?<![a-z])(" + "|".join(re.escape(m) for m in _IN_PERSON) + r")(?![a-z])", re.I)


def _has_in_person(text: str) -> str | None:
    """Return the matched in-person marker, or None. Text is any subject/body."""
    t = _norm(text)
    if not t:
        return None
    m = _IN_PERSON_RE.search(t)
    if m:
        return m.group(1)
    if "conference" in t:
        if any(b in t for b in _CONFERENCE_BAD):
            return None
        if any(g in t for g in _CONFERENCE_OK):
            return "conference"
    return None


def _evidence(subject: str, description: str, marker: str | None) -> str:
    """The text the UI will quote to justify the verdict — it must contain the marker.

    The gate matches the in-person marker against subject OR description, but only ever
    stored the subject: an Event with a blank Subject and the marker in its body produced
    status "done" with evidence "" — a confident assertion citing nothing — and the commoner
    Subject "Meeting" produced a tooltip quoting "Meeting", which justifies nothing. When the
    marker lives in the body we quote a window of the BODY around the match, subject-prefixed
    for context. `evidence` is never empty on a done/planned verdict.
    """
    subj = re.sub(r"\s+", " ", str(subject or "")).strip()
    body = re.sub(r"\s+", " ", str(description or "")).strip()
    if marker and subj and _has_in_person(subj):
        return subj[:200]                       # marker is in the subject — quote it as-is
    if marker and body:
        m = re.search(re.escape(marker), body, re.I)
        if m:
            # ~180 chars of context centred on the match, leaving room for the subject prefix.
            lo, hi = max(0, m.start() - 80), min(len(body), m.end() + 100)
            snip = ("…" if lo else "") + body[lo:hi] + ("…" if hi < len(body) else "")
            out = f"{subj} — {snip}" if subj else snip
            return out[:200]
    return (subj or body)[:200]


def _is_virtual(description: str, location: str, description_raw=None) -> bool:
    """True when the record carries a virtual-join tell and nothing overrides it.

    The JOIN TELL is read from the RAW description, the PHYSICAL OVERRIDE from the cleaned
    one, and that asymmetry is the whole point. Stripping Avoma '##' note bodies removed the
    marker-shaped noise they carry, but it also removed the Teams link that lived below the
    heading — so the veto went blind exactly where the marker survived in the SUBJECT and
    could not be cleaned away. SGD Pharma flipped planned -> done on
    "Avoma - : SGD - Preparation of our on Site WS (28th and 29th of October)": a PREPARATION
    CALL about an onsite, asserting a date that is not the onsite date, whose Teams link sat
    in the stripped body. A join link ANYWHERE is a join link, so the tell reads raw.
    The override stays on the cleaned text on purpose: "at our office" / "in the office"
    inside a '##' body describes the CUSTOMER'S premises (the Actylis failure mode), and must
    never be allowed to cancel a real join link.
    """
    raw = description_raw if description_raw is not None else description
    loc = _norm(location)
    tell_blob = _norm(raw) + " || " + loc
    if not any(v in tell_blob for v in _VIRTUAL):
        return False
    return not any(p in (_norm(description) + " || " + loc) for p in _PHYSICAL_OVERRIDE)


def _is_aspirational(text: str) -> bool:
    t = _norm(text)
    if not t:
        return False
    if any(a in t for a in _ASPIRATIONAL):
        # Past tense in the same breath rescues it: "met F2F- very positive" beats
        # a stray "invite" elsewhere in a long appended next-step blob.
        return not _PAST_TENSE.search(t)
    return bool(_FUTURE_INTENT.search(t)) and not _PAST_TENSE.search(t)


def is_exec_title(title: str) -> bool:
    """Buyer-side seniority from a free-text CRM title. Junior traps tested first."""
    t = _norm(title)
    if not t or t in ("na", "n/a", "-", "none", "unknown"):
        return False           # literal 'NA' means UNKNOWN, never a valid exec
    if _CHIEF_OFFICER.search(t):
        return True
    for bad in _NOT_EXEC:
        if bad in t:
            # "Director" outranks the junior word when both appear
            # (e.g. "Director & Procurement Officer").
            if "director" in t or "chief" in t or "president" in t or "head of" in t:
                break
            return False
    if _HEAD_PREFIX.match(t):
        return True
    return any(e in t for e in _EXEC)


def is_real_person(name: str, email: str = "") -> bool:
    """Filter out room-booking mailboxes and address-shaped 'Contacts'."""
    n = _norm(name)
    e = _norm(email)
    if not n:
        return False
    if any(w in n for w in _ROOM_WORDS) or any(w in e for w in _ROOM_WORDS):
        return False
    if _ADDRESSY.search(n):
        return False
    return True


def _to_date(v):
    if not v:
        return None
    s = str(v)[:10]
    try:
        return date.fromisoformat(s)
    except Exception:  # noqa: BLE001
        return None


def derive_exec_f2f(*, events=None, tasks=None, next_step: str = "",
                    today: date | None = None) -> dict:
    """Weigh the evidence and return the exec-F2F verdict for one deal.

    events: [{subject, description, location, date, attendees:[{name,title,email}],
              description_raw}] — `description` is cleaned (marker matching), `description_raw`
             is the untouched body (virtual veto only); see _is_virtual.
    tasks:  [{subject, status, date}]
    next_step: Opportunity.Next_Step__c raw text.

    Returns {status, date, exec_name, exec_title, evidence, days_stale,
             near_miss, attendees_found}.  `near_miss` = in-person CONFIRMED but no
    executive attendee resolvable — the "it happened, seniority unproven" bucket,
    which is where the real forecast exposure sits (measured: $8.4M over 7 deals).
    """
    today = today or datetime.now(timezone.utc).date()
    events = events or []
    tasks = tasks or []

    best = None        # a qualifying past in-person meeting WITH an exec
    near = None        # a qualifying past in-person meeting WITHOUT an exec
    planned = None     # a future / aspirational in-person signal

    for ev in events:
        subj = ev.get("subject") or ""
        marker = _has_in_person(subj) or _has_in_person(ev.get("description") or "")
        if not marker:
            continue
        when = _to_date(ev.get("date"))
        blob = f"{subj} {ev.get('description') or ''}"
        cite = _evidence(subj, ev.get("description") or "", marker)
        if when and when > today:
            if planned is None:
                planned = (when, cite)
            continue
        # description_raw is the UNCLEANED body, supplied by both writers via the shared prep.
        # Absent (older callers / tests), the cleaned text stands in — the pre-existing
        # behaviour, never a silently weaker veto.
        if _is_virtual(ev.get("description") or "", ev.get("location") or "",
                       ev.get("description_raw")):
            continue
        if _is_aspirational(blob):
            if planned is None:
                planned = (when, cite)
            continue
        if when is None:
            continue

        people = [a for a in (ev.get("attendees") or [])
                  if is_real_person(a.get("name"), a.get("email"))]
        execs = [a for a in people if is_exec_title(a.get("title"))]
        if execs:
            cand = (when, execs[0], cite, len(people))
            if best is None or when > best[0]:
                best = cand
        elif near is None or (when > near[0]):
            near = (when, cite, len(people))

    if best:
        when, ex, cite, n = best
        return {"status": STATUS_DONE, "date": when.isoformat(),
                "exec_name": ex.get("name"), "exec_title": ex.get("title"),
                "evidence": cite, "days_stale": (today - when).days,
                "near_miss": False, "attendees_found": n}

    # No exec-backed meeting. Look for a planned signal in tasks / next-step.
    for tk in tasks:
        subj = tk.get("subject") or ""
        if not _has_in_person(subj):
            continue
        when = _to_date(tk.get("date"))
        status = _norm(tk.get("status"))
        if status in ("completed", "closed") and when and when <= today \
                and not _is_aspirational(subj):
            # A completed in-person Task with no attendee data — evidence it happened,
            # but seniority is unproven, so this is a near-miss, never a "done".
            if near is None or when > near[0]:
                near = (when, subj.strip()[:200], 0)
            continue
        if planned is None:
            planned = (when, subj.strip()[:200])

    if next_step:
        for line in re.split(r"[|\n]", str(next_step)):
            marker = _has_in_person(line)
            if not marker:
                continue
            if planned is None:
                planned = (None, line.strip()[:200])
            break

    if near:
        # In-person is CONFIRMED here — but with no executive attendee resolvable we
        # will not call it done. Reported as planned + near_miss so the UI can show
        # "in person, exec unproven" rather than silently crediting the meeting.
        when, cite, n = near
        return {"status": STATUS_PLANNED,
                "date": when.isoformat() if when else None,
                "exec_name": None, "exec_title": None, "evidence": cite,
                "days_stale": (today - when).days if when else None,
                "near_miss": True, "attendees_found": n}

    if planned:
        when, cite = planned
        return {"status": STATUS_PLANNED,
                "date": when.isoformat() if when else None,
                "exec_name": None, "exec_title": None, "evidence": cite,
                "days_stale": None, "near_miss": False, "attendees_found": 0}

    return {"status": STATUS_NONE, "date": None, "exec_name": None,
            "exec_title": None, "evidence": None, "days_stale": None,
            "near_miss": False, "attendees_found": 0}
