"""Shared data-prep for the exec-F2F verdict — the ONE place the two writers agree.

Two writers feed ai.exec_f2f: the nightly sweep (deal_engine_sweep._exec_f2f_for) and the
backfill (f2f_rebuild_all). They drifted, and every drift was measured damage:
  - the sweep read LAST_N_DAYS:365 while the backfill read 730, so the first sweep after a
    backfill overwrote the rich verdict with a thin one (6 of 10 collapsed to status "none",
    which the UI renders as "No evidence" — the exact false blank this column exists to prevent);
  - opposite ActivityDate/ActivityDateTime precedence flipped the asserted day (Orora
    2025-11-25 vs 11-26, ARUP 2026-01-09 vs 01-10).
Window, date precedence, description cleaning and the overwrite rank live here so they cannot
diverge again. Import this, never re-implement it.
"""
from __future__ import annotations

import re

# ONE window for BOTH writers. 730d, not 365d: this asks "has an exec F2F happened at all
# this cycle", so a real onsite 8 months ago is still the answer — and the verdict's own
# `days_stale` is what flags an old one as stale.
F2F_WINDOW_DAYS = 730

# ── the SOQL shape, shared so the two writers cannot select different rows ────────────
# Sharing the CONSTANT was not enough: the sweep filtered on ActivityDateTime alone while
# the backfill filtered on (ActivityDateTime OR ActivityDate). All-day Events have a NULL
# ActivityDateTime — 2 in the active book, 4 org-wide — so they were visible to the backfill
# and invisible to the sweep, and each writer then overwrote the other. Likewise the ORDER BY
# (ActivityDateTime DESC vs ActivityDate DESC) and the caps (200 vs 200/400) diverged, so on
# the 19 opps with >200 events and the 181 with >200 tasks the two writers read a DIFFERENT
# 200. Predicate, ordering and caps all live here; neither writer may re-implement them.
MAX_EVENTS = 200
MAX_TASKS = 400

# ActivityDate first, matching event_date() below — the ordering must agree with the date we
# actually assert, or "most recent 200" is sorted on a field the verdict does not use.
EVENT_ORDER_BY = "ORDER BY ActivityDate DESC NULLS LAST"
TASK_ORDER_BY = "ORDER BY ActivityDate DESC NULLS LAST"


def event_window_clause(prefix: str = "") -> str:
    """SOQL window predicate for Events. `prefix` qualifies the fields for a related-object
    query (EventRelation filters on "Event.ActivityDate..."), so the relationship hop reads
    exactly the same population as the Event query itself."""
    p = f"{prefix}." if prefix else ""
    return (f"({p}ActivityDateTime >= LAST_N_DAYS:{F2F_WINDOW_DAYS} "
            f"OR {p}ActivityDate >= LAST_N_DAYS:{F2F_WINDOW_DAYS})")


def task_window_clause(prefix: str = "") -> str:
    """SOQL window predicate for Tasks. Task has no ActivityDateTime — ActivityDate is the
    only date it carries — but this stays a function so both writers call one name."""
    p = f"{prefix}." if prefix else ""
    return f"{p}ActivityDate >= LAST_N_DAYS:{F2F_WINDOW_DAYS}"


def event_date(activity_date, activity_datetime):
    """The date to assert for an Event. ActivityDate FIRST: ActivityDateTime is UTC and lands
    on the wrong calendar day either side of the date line, while ActivityDate is the local
    day a human would actually name the meeting."""
    return activity_date or activity_datetime


# Avoma AI notes append '## Purpose' / '## Key takeaways' / '## Next steps' sections to the
# invite body. Headings are line-anchored so a '##' inside prose can never split the text.
_AVOMA_HEADING = re.compile(r"(?m)^\s*##")

# URLs are machine text, never prose, and their paths carry marker-shaped noise: the Avoma
# link https://app.avoma.com/meetings/82f42066-450f-4f2f-b03a-… contains "4f2f", which the
# word-boundary marker regex reads as a genuine "f2f" (digits and hyphens are not [a-z], so
# the boundary holds). That alone credited International SOS with an in-person exec meeting.
# Truncating to scheme+host kills the path noise and KEEPS every virtual tell, all of which
# are hostnames (zoom.us, teams.microsoft.com, webex.com, meet.google) — so the veto is
# untouched. Prose tells ("Meeting ID:", "passcode:") never live in a URL either.
_URL = re.compile(r"https?://[^\s<>\"'()\[\]]+", re.I)


def _host_only(m):
    u = m.group(0)
    cut = u.find("/", len("https://"))
    return u[:cut] if cut > 0 else u


def clean_description(description):
    """Strip an Avoma AI-note body back to the real invite text, and URLs back to their host.

    The '##' sections describe the CUSTOMER'S business, not the meeting: Actylis was credited
    with a completed in-person executive meeting off "Sage X3 ERP went live on site in
    February '22" and "weekly on-site visits to review open invoices", inside what is actually
    a virtual "Introductory Call". 143 events across the book carry their only in-person
    marker inside such a body. Only the portion BEFORE the first '##' heading is the invite —
    and it still carries the join link, so the virtual veto keeps working. Contrast Bufab, a
    true positive: its marker sits in a Clari invite with a Malmo hotel address, a booked
    conference room and catering, all above any heading.
    """
    s = str(description or "")
    m = _AVOMA_HEADING.search(s)
    return _URL.sub(_host_only, s[:m.start()] if m else s)


# done > planned > none. Anything unrecognised ranks below "none" so it can never clobber.
_RANK = {"done": 3, "planned": 2, "none": 1}


def should_replace(new_verdict, existing_verdict) -> bool:
    """True when `new_verdict` may overwrite a STORED ai.exec_f2f.

    derive_exec_f2f never returns {} — a no-evidence run returns {"status": "none", ...},
    which is truthy — so the sweep's `if _f2f_v:` guard was ALWAYS true and a thin run
    silently downgraded a rich stored verdict. Equal rank still refreshes (dates and
    days_stale move); a weaker one is dropped, so "none" can never clobber "done"/"planned".
    """
    if not isinstance(new_verdict, dict) or not new_verdict.get("status"):
        return False
    if not isinstance(existing_verdict, dict) or not existing_verdict.get("status"):
        return True
    return _RANK.get(new_verdict.get("status"), 0) >= _RANK.get(existing_verdict.get("status"), 0)
