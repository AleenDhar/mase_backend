"""Authoritative, server-computed engagement *pulse* for one opportunity.

ONE today-anchored read of how recently and meaningfully a deal is being worked,
derived purely from VERIFIED signals (Salesforce LastActivityDate, the buyer
calls read this sweep, stage/close proximity, forecast) PLUS recent dated
rep-initiated outreach parsed from the Next Step field. The rep outreach is
classified DISTINCTLY ("rep reached out, awaiting buyer reply") so it is never
mistaken for a two-way buyer touch.

Every sweep section (verdict, best-practices, requirements, recommended moves)
and every derived view (Espresso to-dos, Matcha stalled rollup) reads this one
pulse, so they can no longer contradict each other or today's date. The module
is deterministic and stdlib-only, so it is safe to import from the sweep, the
store, and tests alike.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import Any, Optional

# Engagement-state thresholds (env-tunable). Anchored to today.
#   live    : verified buyer/SF activity within LIVE_DAYS
#   cooling : verified activity older than LIVE_DAYS but within DARK_DAYS, OR a
#             genuinely dark deal that nonetheless has a recent rep outreach
#   dark    : no verified activity within DARK_DAYS and no recent rep outreach
LIVE_DAYS = int(os.getenv("DEAL_PULSE_LIVE_DAYS", "30"))
DARK_DAYS = int(os.getenv("DEAL_PULSE_DARK_DAYS", "90"))
# A dated touch in the Next Step field this recent counts as a rep-initiated
# outreach ("we reached out, awaiting their reply").
REP_OUTREACH_DAYS = int(os.getenv("DEAL_PULSE_REP_OUTREACH_DAYS", "30"))

REP_OUTREACH_NOTE = "rep reached out, awaiting buyer reply"


def _parse_date(v: Any) -> Optional[date]:
    """Parse an ISO date/datetime string to a date; None on anything else."""
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str) and len(v) >= 10:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return date.fromisoformat(v[:10])
            except ValueError:
                return None
    return None


def _days_since(d: Optional[date], today: date) -> Optional[int]:
    return (today - d).days if d is not None else None


# ---------------------------------------------------------------------------
# Next Step rep-outreach parsing
# ---------------------------------------------------------------------------

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_ISO_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_NUM_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_DM_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sept|sep|oct|nov|dec)"
    r"[a-z]*\.?(?:,?\s*(\d{4}))?\b",
    re.I,
)
_MD_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sept|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?\b",
    re.I,
)


def _mk_date(year: Optional[int], month: int, day: int, today: date) -> Optional[date]:
    """Build a date, inferring a missing/short year. When the year is omitted we
    assume the current year, rolling back one year if that would land more than a
    couple of days in the future (so a "Dec 30" seen in January reads as last
    year, not next)."""
    try:
        if year is None:
            d = date(today.year, month, day)
            if (d - today).days > 2:
                d = date(today.year - 1, month, day)
            return d
        if year < 100:
            year += 2000
        return date(year, month, day)
    except ValueError:
        return None


def _candidate_dates(text: str, today: date) -> list[date]:
    out: list[date] = []
    for m in _ISO_RE.finditer(text):
        d = _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)), today)
        if d:
            out.append(d)
    for m in _NUM_RE.finditer(text):
        a, b = int(m.group(1)), int(m.group(2))
        yr = int(m.group(3)) if m.group(3) else None
        # US month/day; if the first number can't be a month, swap.
        month, day = (a, b) if a <= 12 else (b, a)
        d = _mk_date(yr, month, day, today)
        if d:
            out.append(d)
    for m in _DM_RE.finditer(text):
        mon = _MONTHS.get(m.group(2).lower())
        if mon:
            yr = int(m.group(3)) if m.group(3) else None
            d = _mk_date(yr, mon, int(m.group(1)), today)
            if d:
                out.append(d)
    for m in _MD_RE.finditer(text):
        mon = _MONTHS.get(m.group(1).lower())
        if mon:
            yr = int(m.group(3)) if m.group(3) else None
            d = _mk_date(yr, mon, int(m.group(2)), today)
            if d:
                out.append(d)
    return out


def parse_rep_outreach(next_step: Any, today: date) -> dict:
    """Detect a recent, dated rep-initiated touch in the Next Step text.

    Returns {detected, date, text, note}. `detected` is True only when the text
    carries a date within the last REP_OUTREACH_DAYS (a small +2d grace for
    timezone skew). A future-planned date is NOT a touch already made, so it does
    not count. This is deliberately distinct from verified buyer engagement."""
    out = {"detected": False, "date": None, "text": None, "note": REP_OUTREACH_NOTE}
    if not isinstance(next_step, str) or not next_step.strip():
        return out
    text = next_step.strip()
    recent = [
        d for d in _candidate_dates(text, today)
        if 0 <= (today - d).days <= REP_OUTREACH_DAYS or 0 <= (d - today).days <= 2
    ]
    if not recent:
        return out
    out["detected"] = True
    out["date"] = max(recent).isoformat()
    out["text"] = text[:300]
    return out


# ---------------------------------------------------------------------------
# Pulse computation
# ---------------------------------------------------------------------------

def compute_pulse(
    *,
    last_activity_date: Any = None,
    calls_read: Optional[int] = None,
    stage: Any = None,
    close_date: Any = None,
    forecast_category: Any = None,
    qualified_date: Any = None,
    next_step: Any = None,
    today: Optional[date] = None,
) -> dict:
    """Compute the single authoritative engagement pulse for one opportunity."""
    today = today or date.today()
    la = _parse_date(last_activity_date)
    days_since = _days_since(la, today)
    cd = _parse_date(close_date)
    qd = _parse_date(qualified_date)
    rep = parse_rep_outreach(next_step, today)

    cr = None if calls_read is None else int(calls_read)
    buyer_calls_seen = bool(cr and cr > 0)

    # Verified-engagement classification, anchored to today. A fresh buyer call
    # this sweep is itself recent verified engagement even if SF LastActivityDate
    # lags (Avoma calls do not always stamp LastActivityDate).
    verified_recent = (days_since is not None and days_since <= LIVE_DAYS) or buyer_calls_seen
    verified_known = days_since is not None or cr is not None

    if verified_recent:
        state = "live"
    elif days_since is not None and days_since <= DARK_DAYS:
        state = "cooling"
    elif rep["detected"]:
        # Verified dark/unknown, but the rep reached out recently — surface that
        # as a cooling signal rather than a flat "nothing happened".
        state = "cooling"
    else:
        state = "dark"

    days_to_close = (cd - today).days if cd is not None else None
    days_since_qualified = _days_since(qd, today)

    pulse = {
        "as_of": today.isoformat(),
        "state": state,
        "last_activity_date": la.isoformat() if la else None,
        "days_since_activity": days_since,
        "calls_read": cr,
        "buyer_calls_seen": buyer_calls_seen,
        "stage": stage or None,
        "days_since_qualified": days_since_qualified,
        "close_date": cd.isoformat() if cd else None,
        "days_to_close": days_to_close,
        "forecast_category": forecast_category or None,
        "rep_outreach": rep,
        "verified_known": verified_known,
        "summary": "",
    }
    pulse["summary"] = _summary(pulse)
    return pulse


def compute_pulse_from_hard(hard: Optional[dict], today: Optional[date] = None,
                            calls_read: Optional[int] = None) -> dict:
    """Build a pulse from a stored record's `hard` block (no live SF read). Used
    by the derived views so they read the SAME pulse shape even for records swept
    before the pulse was introduced."""
    hard = hard or {}
    return compute_pulse(
        last_activity_date=hard.get("last_activity_date"),
        calls_read=calls_read,
        stage=hard.get("stage"),
        close_date=hard.get("close_date"),
        forecast_category=hard.get("forecast_category"),
        qualified_date=hard.get("qualified_date"),
        next_step=hard.get("next_step"),
        today=today,
    )


def _summary(p: dict) -> str:
    state = p.get("state")
    ds = p.get("days_since_activity")
    la = p.get("last_activity_date")
    if state == "live":
        if p.get("buyer_calls_seen") and (ds is None or ds > LIVE_DAYS):
            base = "Live: buyer call(s) read this sweep."
        else:
            base = (f"Live: last verified Salesforce activity {ds} day(s) ago ({la})."
                    if ds is not None else "Live: recent verified engagement.")
    elif state == "cooling":
        if ds is not None:
            base = f"Cooling: last verified activity {ds} day(s) ago ({la})."
        else:
            base = "Cooling: no recent verified buyer engagement."
    else:
        base = (f"Dark: no verified buyer engagement in {ds} day(s) (last {la})."
                if ds is not None else "Dark: no verified buyer engagement on record.")
    rep = p.get("rep_outreach") or {}
    if rep.get("detected"):
        base += f" Rep outreach {rep.get('date')} — {REP_OUTREACH_NOTE}."
    return base


def is_pulse_live(pulse: Optional[dict]) -> bool:
    return bool(pulse) and pulse.get("state") == "live"


# ---------------------------------------------------------------------------
# Stale-worldview flag reconciliation
# ---------------------------------------------------------------------------

# Markers of a stale-worldview best-practice flag: a flag that calls the deal a
# ghost / dark-for-months / future-date-data-quality / wrong-stage problem. When
# the live pulse shows recent verified activity these are categorically wrong and
# must stop projecting as live to-do flags.
_GHOST_MARKERS = (
    "ghost",
    "gone dark", "dark for", "deal is dark", "appears dark", "going dark",
    "future date", "future-date", "future activity date",
    "lastactivitydate is a future", "last activity date is a future",
    "actual last activity",
    "wrong stage", "stage is wrong", "stale stage", "incorrect stage",
    "data quality", "data-quality",
    "no recent activity", "no activity in", "dormant", "stalled out",
)


def flag_contradicts_live_pulse(text: Any, pulse: Optional[dict]) -> bool:
    """True when a best-practice flag asserts a long-dark / future-date /
    wrong-stage condition that the LIVE pulse contradicts. High precision: it
    fires only against an explicitly live pulse and only on stale-worldview
    phrasings, so a legitimate live flag (single-thread, missing EB, ...) is
    never touched."""
    if not is_pulse_live(pulse):
        return False
    t = str(text or "").lower()
    if not t:
        return False
    if any(s in t for s in _GHOST_MARKERS):
        return True
    # "N months since / of silence / no contact" — a months-long gap is a direct
    # contradiction of a live pulse.
    if re.search(r"\d+\s*months?", t) and any(
            k in t for k in ("since", "silence", "no contact", "no buyer",
                             "without", "ago", "dark", "stall", "engage")):
        return True
    # "N days since last buyer touch" where N clearly exceeds the live window.
    m = re.search(r"(\d+)\s*days?", t)
    if m and any(k in t for k in ("since last", "no contact", "no buyer",
                                  "silence", "without", "dark", "no activity")):
        try:
            if int(m.group(1)) > LIVE_DAYS:
                return True
        except ValueError:
            pass
    return False


# ---------------------------------------------------------------------------
# Prompt block
# ---------------------------------------------------------------------------

def render_block(pulse: dict) -> str:
    """Render the pulse as a ground-truth block for the sweep prompt."""
    state = (pulse.get("state") or "unknown").upper()
    lines = [
        "=== GROUND TRUTH — engagement pulse (server-computed, today-anchored) ===",
        "This is the single authoritative read of how recently and meaningfully "
        "this deal is being worked, computed by the server from verified signals. "
        "EVERY section you emit (verdict, best-practice flags, explicit/implicit "
        "requirements, recommended moves) MUST be consistent with this pulse and "
        "with today's date. Do NOT call this deal a ghost, dark-for-months, a "
        "future-date data-quality problem, or wrong-stage when the pulse shows "
        "recent verified activity; and do NOT ignore a recent rep-initiated "
        "outreach.",
        f"- Today: {pulse.get('as_of')}",
        f"- Engagement state: {state}",
    ]
    ds = pulse.get("days_since_activity")
    if pulse.get("last_activity_date"):
        lines.append(
            f"- Last verified Salesforce activity (LastActivityDate): "
            f"{pulse.get('last_activity_date')}"
            + (f" ({ds} day(s) ago)" if ds is not None else ""))
    else:
        lines.append("- Last verified Salesforce activity (LastActivityDate): none on record")
    if pulse.get("calls_read") is not None:
        lines.append(f"- Buyer calls read this sweep: {pulse.get('calls_read')}")
    if pulse.get("stage"):
        lines.append(f"- Stage: {pulse.get('stage')}")
    if pulse.get("days_to_close") is not None:
        lines.append(f"- Days to close: {pulse.get('days_to_close')} "
                     f"(close {pulse.get('close_date')})")
    rep = pulse.get("rep_outreach") or {}
    if rep.get("detected"):
        lines.append(
            f"- Recent rep-initiated outreach (from Next Step, NOT verified buyer "
            f"engagement): dated {rep.get('date')} — treat as \"{REP_OUTREACH_NOTE}\", "
            f"distinct from a two-way buyer touch. Surface this instead of "
            f"\"no activity in N days\".")
    if state == "DARK":
        lines.append(
            "- This deal is genuinely dark: no verified buyer engagement in the "
            "window and no recent rep outreach. The correct action is to "
            "RE-ENGAGE, not to chase a stale year-old deliverable.")
    return "\n".join(lines)
