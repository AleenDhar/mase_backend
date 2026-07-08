"""Deterministic opportunity-level TREND signals from Salesforce field history.

The rep's CRM moves are themselves buying/loss signals (2026-06-29, user-directed):
a deal whose AMOUNT goes up, CLOSE DATE is pulled earlier, STAGE advances, or
FORECAST CATEGORY is upgraded is winning ground; the reverse (amount cut, close date
pushed out, stage/category regressed) is losing it. These are computed deterministically
from `field_history_cache` (1 row per field change) — NO LLM — and feed Zycus Win Position
as signed signals within its +/-30 rubric band.

Output: a dict of signed strengths in [-1, +1] per trend, e.g.
    {"amount_trend": +0.6, "close_date_trend": -1.0, "stage_trend": -1.0, ...}
positive = progression (good for win), negative = regression.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Stage order (ascending = further through buying). Mirrors WIN_STAGE_ANCHOR ordering.
_STAGE_RANK = {
    "initial interest": 0, "qualified": 1, "formal evaluation": 2, "evaluation": 2,
    "shortlisted": 3, "vendor selected": 4, "selected": 4,
    "contract in progress": 5, "negotiation": 5, "contracting": 5,
    "contract signed": 6, "po received": 7,
}
# Dead/regressed terminal stages → strong regression.
_STAGE_DEAD = {"no decision", "qualified out", "closed lost", "lost", "omitted", "dropped"}

# Forecast category order (ascending strength). Used when FC history is available.
# Forecast-category confidence order (ascending). Zycus: Omitted < Pipeline < Upside(/Key Deal)
# < Best Case < Commit. "Upside Key Deal" ranks BELOW "Best Case" — a Best Case -> Upside move is a
# DOWNGRADE (reduced confidence), NOT an upgrade. The old map had Upside(3) ABOVE Best Case(2), so a
# downgrade scored as +progression on momentum and +nudge on win (Austrian Post: cut to Upside the
# same week the amount dropped 31% and the close slipped, yet read as "forecast moved UP").
_FC_RANK = {"omitted": 0, "pipeline": 1, "upside": 2, "upside key deal": 2,
            "best case": 3, "commit": 4}

_TREND_WINDOW_DAYS = 120        # only consider changes within this window
_RECENCY_TAU_DAYS = 60.0       # a change decays to ~0.37 strength by this age


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        try:
            return datetime.fromisoformat(str(s)[:19])
        except Exception:  # noqa: BLE001
            return None


def _age_days(changed_at, now=None):
    dt = _parse_dt(changed_at)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def _recency(age_days):
    """1.0 for a change today, decaying with age (so a stale move barely counts)."""
    import math
    if age_days is None:
        return 0.5
    return math.exp(-age_days / _RECENCY_TAU_DAYS)


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _latest_change(rows, field):
    """Most recent in-window change for `field` (rows are field_history_cache dicts)."""
    best = None
    for r in rows:
        if (r.get("field_name") or "") != field:
            continue
        age = _age_days(r.get("changed_date"))
        if age is None or age > _TREND_WINDOW_DAYS:
            continue
        if best is None or age < best[0]:
            best = (age, r)
    return best  # (age_days, row) or None


def _signed(delta_sign, age_days, mag=1.0):
    """A signed strength = direction * magnitude * recency, clamped [-1,1]."""
    s = delta_sign * mag * _recency(age_days)
    return max(-1.0, min(1.0, round(s, 3)))


def derive_opp_trends(history_rows: list) -> dict:
    """Compute signed trend signals from a list of field_history_cache rows for ONE opp.
    Empty/over-age fields are simply absent (no signal)."""
    out = {}
    rows = history_rows or []

    # --- Amount: up = good, down = bad ---
    a = _latest_change(rows, "Amount")
    if a:
        age, r = a
        old, new = _num(r.get("old_value")), _num(r.get("new_value"))
        if old is not None and new is not None and old != new:
            # magnitude scales with the relative size of the move (capped).
            rel = abs(new - old) / max(abs(old), 1.0)
            mag = min(1.0, 0.4 + rel)            # any real move is meaningful; big moves stronger
            out["amount_trend"] = _signed(1.0 if new > old else -1.0, age, mag)
            out["amount_trend_detail"] = f"Amount {old:g} -> {new:g} ({int(age)}d ago)"

    # --- Close date: pulled EARLIER = good, pushed OUT = bad ---
    c = _latest_change(rows, "CloseDate")
    if c:
        age, r = c
        od, nd = _parse_dt(r.get("old_value")), _parse_dt(r.get("new_value"))
        if od and nd and od.date() != nd.date():
            pulled_in = nd < od
            days = abs((nd - od).days)
            mag = min(1.0, 0.4 + days / 60.0)
            out["close_date_trend"] = _signed(1.0 if pulled_in else -1.0, age, mag)
            out["close_date_trend_detail"] = (
                f"Close date {od.date()} -> {nd.date()} "
                f"({'pulled in' if pulled_in else 'pushed out'} {days}d)")

    # --- Stage: advance = good, regress / dead = bad ---
    s = _latest_change(rows, "StageName")
    if s:
        age, r = s
        old = str(r.get("old_value") or "").strip().lower()
        new = str(r.get("new_value") or "").strip().lower()
        if old != new:
            if new in _STAGE_DEAD:
                out["stage_trend"] = _signed(-1.0, age, 1.0)
            else:
                ro, rn = _STAGE_RANK.get(old), _STAGE_RANK.get(new)
                if ro is not None and rn is not None and ro != rn:
                    mag = min(1.0, 0.5 + 0.25 * abs(rn - ro))
                    out["stage_trend"] = _signed(1.0 if rn > ro else -1.0, age, mag)
            if "stage_trend" in out:
                out["stage_trend_detail"] = f"Stage {old} -> {new} ({int(age)}d ago)"

    # --- Forecast category: upgrade = good, downgrade = bad (when history present) ---
    f = _latest_change(rows, "ForecastCategoryName") or _latest_change(rows, "ForecastCategory")
    if f:
        age, r = f
        ro = _FC_RANK.get(str(r.get("old_value") or "").strip().lower())
        rn = _FC_RANK.get(str(r.get("new_value") or "").strip().lower())
        if ro is not None and rn is not None and ro != rn:
            mag = min(1.0, 0.5 + 0.25 * abs(rn - ro))
            out["forecast_category_trend"] = _signed(1.0 if rn > ro else -1.0, age, mag)
            out["forecast_category_trend_detail"] = (
                f"Forecast {r.get('old_value')} -> {r.get('new_value')} ({int(age)}d ago)")

    return out
