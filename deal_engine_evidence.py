"""Deterministic EVIDENCE PACKET for AI deal-scoring.

The packet is FACTS only — no judgment. It assembles, for one opportunity, the complete
engagement picture the old deterministic scorer kept missing:

  * MEETINGS from the Avoma datalake (the source of truth) — a buyer-attended meeting is
    first-class engagement (the old code counted only emails, so a deal with 19 buyer
    meetings read "0 buyer touches").
  * SFDC engagement — Next Step (dated), last activity, and (when a live sweep supplies
    them) Tasks/Events.
  * Deal mechanics + the qualitative context the sweep already produced (trends, MEDDPICC,
    competition, champion, decision outcome, vulnerabilities).

`build_evidence_packet()` returns a compact dict the AI scorer reasons over. Counts/dates
stay here (deterministic) so the model judges but never invents numbers.
"""
from __future__ import annotations
import os
import re
import json
import ssl
import urllib.request
import urllib.parse
import datetime
from typing import Optional

_LATE = ("vendor selected", "contract", "negotiat", "verbal", "closed won", "selected", "legal", "signature")
_MID = ("shortlist", "formal evaluation", "evaluation", "proposal", "poc", "pilot", "demo", "business case")


def _today() -> datetime.date:
    return datetime.date.today()


def _stage_tier(stage: str) -> str:
    s = (stage or "").strip().lower()
    if any(w in s for w in _LATE):
        return "late"
    if any(w in s for w in _MID):
        return "mid"
    return "early"


def _days_since(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        return (_today() - datetime.date.fromisoformat(str(iso)[:10])).days
    except Exception:
        return None


def _days_until(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        return (datetime.date.fromisoformat(str(iso)[:10]) - _today()).days
    except Exception:
        return None


def _strip_html(s) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", str(s))
    s = s.replace("&amp;", "&").replace("&nbsp;", " ").replace("&gt;", ">").replace("&lt;", "<")
    return re.sub(r"\s+", " ", s).strip()


# ---- datalake meetings (REST; DATALAKE_URL + DATALAKE_SERVICE_KEY from env) ----------
_GENERIC_DOMAINS = {"zycus.com", "gmail.com", "outlook.com", "hotmail.com", "yahoo.com",
                    "googlemail.com", "icloud.com", "live.com", "aol.com"}


def _datalake_ctx():
    bundle = os.getenv("CORP_CA_BUNDLE") or r"C:/Users/Aleen.Dhar/.aws/corp-ca-bundle.pem"
    ctx = ssl.create_default_context(cafile=bundle) if os.path.exists(bundle) else ssl.create_default_context()
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def fetch_datalake_meetings(opp_id: str, *, limit: int = 300) -> list[dict]:
    """HELD, non-internal meetings for an opp from the datalake. [] on any failure.

    2026-07-09 (John Deere): `state=eq.completed` silently ERASED held-but-not-recorded
    meetings — John Deere's four 2026 meetings (11 May, 31 Mar, 24 Feb, 28 Jan) are all
    `not_recorded`, so the packet told the scorer "0 meetings in 90d / last meeting Dec-18 /
    203 days of silence" while the buyer had met Zycus 59 days earlier. A meeting the bot
    didn't record STILL HAPPENED (`not_recorded` ≠ didn't happen — the anti-fabrication
    rule). Include not_recorded; future-dated rows are dropped in _meetings_block."""
    url = (os.getenv("DATALAKE_URL") or "").rstrip("/")
    key = os.getenv("DATALAKE_SERVICE_KEY") or ""
    if not url or not key or not opp_id:
        return []
    sel = "uuid,subject,start_at,is_internal,is_call,state,transcript_ready,duration,crm_opportunity_id,attendee_domains"
    qs = (f"avoma_meetings?select={sel}&crm_opportunity_id=ilike.{urllib.parse.quote(opp_id[:15])}*"
          f"&state=in.(completed,not_recorded)&is_internal=eq.false&order=start_at.desc&limit={limit}")
    req = urllib.request.Request(f"{url}/rest/v1/{qs}", headers={"apikey": key, "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_datalake_ctx()) as r:
            return json.load(r) or []
    except Exception:
        return []


def _meetings_block(opp_id: str, meetings: Optional[list[dict]]) -> dict:
    rows = meetings if meetings is not None else fetch_datalake_meetings(opp_id)
    rows = [m for m in rows if m.get("start_at") and not m.get("is_internal")]
    # HELD meetings only: a datalake row with a FUTURE start is a scheduled session,
    # not engagement that happened — count it separately as next_scheduled.
    _past = [m for m in rows if (_days_since(str(m["start_at"])[:10]) or -1) >= 0]
    _future = sorted((str(m["start_at"])[:10] for m in rows
                      if (_days_since(str(m["start_at"])[:10]) or 0) < 0))
    # one entry per calendar day (same session is logged multiple times)
    by_day = {}
    for m in _past:
        k = str(m["start_at"])[:10]
        if k not in by_day:
            by_day[k] = m
    days = sorted(by_day.keys(), reverse=True)

    def _in(n):
        return sum(1 for d in days if (_days_since(d) or 9999) <= n)

    recent = []
    for d in days[:12]:
        m = by_day[d]
        doms = [x for x in (m.get("attendee_domains") or []) if str(x).lower() not in _GENERIC_DOMAINS]
        recent.append({
            "date": d,
            "subject": (m.get("subject") or "")[:90],
            "buyer_domains": doms[:3],
            "recorded": (m.get("state") == "completed"),
            "transcript": bool(m.get("transcript_ready")),
            "minutes": round((m.get("duration") or 0) / 60) if m.get("duration") else None,
        })
    return {
        "total_all_time": len(days),
        "count_30d": _in(30), "count_60d": _in(60), "count_90d": _in(90),
        "with_transcript_all_time": sum(1 for d in by_day.values() if d.get("transcript_ready")),
        "last_date": days[0] if days else None,
        "days_since_last": _days_since(days[0]) if days else None,
        "next_scheduled": (_future[0] if _future else None),
        "recent": recent,
    }


def _meddpicc_block(ai: dict) -> dict:
    md = ai.get("meddpicc") or {}
    out = {}
    for k, v in md.items():
        if isinstance(v, dict):
            st = v.get("status")
            if st:
                out[k] = st
    return out


def _competition_block(ai: dict) -> dict:
    cp = ai.get("competitive_position") or {}
    comps = []
    for c in (cp.get("competitors") or []):
        if isinstance(c, dict) and c.get("name"):
            comps.append({"name": c.get("name"), "threat": c.get("threat_level"), "status": c.get("status")})
    return {"summary": _strip_html(cp.get("summary"))[:400], "competitors": comps[:5]}


def build_evidence_packet(record: dict, *, meetings: Optional[list[dict]] = None,
                          sf_activities: Optional[dict] = None) -> dict:
    """Assemble the deterministic evidence packet for one deal. `meetings` (datalake rows)
    and `sf_activities` ({tasks, events, next_step_history}) are passed in by a live sweep;
    when omitted, meetings are fetched from the datalake and SFDC context is taken from the
    stored record (next_step, last_activity, footprints)."""
    ai = record.get("ai") or {}
    hard = record.get("hard") or {}
    opp_id = record.get("opp_id") or hard.get("opp_id") or ""
    stage = hard.get("stage") or ""
    fp = ai.get("footprints") or {}

    packet = {
        "deal": {
            "opp_id": opp_id,
            "account": hard.get("account_name"),
            "opp_name": hard.get("opp_name"),
            "stage": stage,
            "stage_tier": _stage_tier(stage),
            "amount": hard.get("amount"),
            "forecast_category": hard.get("forecast_category"),
            "close_date": hard.get("close_date"),
            "days_to_close": _days_until(hard.get("close_date")),
            # 2026-07-09 (S&C/SAMI CRO-judge): 600 chars TRUNCATED the forward plan — S&C's four
            # booked 13-17 Jul demos and SAMI's 2/7-Jul buyer proposal-chases live in Next Step /
            # Next Step History and were CUT, so the momentum scorer said "no dated future meeting"
            # and under-read active deals by ~15-24. Raise to 2400; append the history trail.
            "next_step": _strip_html(hard.get("next_step"))[:2400],
            "next_step_history": (_strip_html(((sf_activities or {}).get("next_step_history")) or "")[:1800]
                                  or None),
            "last_activity_date": hard.get("last_activity_date") or fp.get("general_last_activity"),
        },
        "meetings": _meetings_block(opp_id, meetings),
        "trends": ai.get("opp_trends") or {},
        "meddpicc": _meddpicc_block(ai),
        "competition": _competition_block(ai),
        "champion": _strip_html((ai.get("champion_strength") or {}).get("summary")
                                or (ai.get("champion_strength") or {}).get("strength"))[:300],
        "decision_outcome": ai.get("decision_outcome") or {},
        "vulnerabilities": [_strip_html(v.get("detail"))[:160]
                            for v in ((ai.get("vulnerabilities") or {}).get("items") or [])][:5],
    }

    # Clean engagement read: MEETINGS are the engagement truth (a buyer-attended meeting is
    # a two-way touch). The stored footprint buyer_touches / last_buyer_touch are deliberately
    # NOT surfaced — they were the bug (0 despite many meetings; last touch defaulted to today).
    mt = packet["meetings"]
    _eng = fp.get("engagement") or {}
    packet["buyer_engagement"] = {
        "meeting_days_30d": mt["count_30d"],
        "meeting_days_60d": mt["count_60d"],
        "days_since_last_meeting": mt["days_since_last"],
        "sfdc_email_touches_60d": fp.get("buyer_touches_60d"),
        "buyer_touches_30d": fp.get("buyer_touches_30d"),
        "rep_only": fp.get("rep_only"),
        # 2026-07-09: the measured engagement DEPTH the momentum scorer was flying blind to —
        # its own points read (type × who × recency) + the forward calendar. Without these the
        # AI scorer said "no future meeting" while demos were booked (S&C under-read 92->68).
        "engagement_points_60d": _eng.get("points_60d"),
        "next_scheduled_meeting": mt.get("next_scheduled"),
    }
    # FORWARD MOMENTUM (2026-07-09): explicit forward-motion signals so the scorer credits a
    # LIVE advancing plan (booked demos, a dated buyer milestone) instead of chipping momentum
    # for "no future meeting" when the calendar is full. Sourced from the datalake future rows
    # + the (now un-truncated) next step; the scorer still weighs BUYER-accepted forward dates,
    # not rep intentions.
    packet["forward_momentum"] = {
        "next_scheduled_meeting": mt.get("next_scheduled"),
        "has_future_calendar": bool(mt.get("next_scheduled")),
        "engagement_points_60d": _eng.get("points_60d"),
    }

    # Differentiation / fit signals already produced by the sweep (Win §7 factor 1 +
    # the preference read). Surfaced here, not re-fetched — the sweep still gathers them.
    def _summ(v):
        if isinstance(v, dict):
            return _strip_html(v.get("summary") or v.get("strength") or v.get("signal") or "")[:240] or None
        return _strip_html(v)[:240] if v else None
    packet["fit"] = {
        "ai_fit_signal": _summ(ai.get("ai_fit_signal")),
        "positioning_strength": _summ(ai.get("ai_positioning_strength")),
        "expectations_fit": _summ(ai.get("customer_expectations_fit")),
        "north_star_verdict": _summ(ai.get("north_star_verdict")),
    }

    # Live-sweep SFDC activities (richest signal) override the stored engagement snapshot.
    if sf_activities:
        packet["sf_activities"] = {
            "tasks": (sf_activities.get("tasks") or [])[:40],
            "events": (sf_activities.get("events") or [])[:40],
            "next_step_history": _strip_html(sf_activities.get("next_step_history"))[:600] or None,
        }

    # SWEEP ANALYSIS CONTEXT (2026-07-15): the full narrative analysis produced by the
    # evidence-extraction pass. The scoring LLM receives this so its scores, verdict, and
    # recommended_moves are coherent with the same stakeholder / champion / EB reads that
    # appear in the drawer — preventing the "scoring is a separate entity / data mismatch"
    # problem reported on SABIC. Fields are pass-through (no truncation beyond lists),
    # because the scorer MUST use the same picture the analysis produced.
    _analysis_ctx: dict = {}
    # 24-hour summary — buyer actions since last sweep
    _ds = ai.get("day_summary")
    if isinstance(_ds, dict) and _ds.get("items"):
        _analysis_ctx["day_summary"] = {
            "as_of": _ds.get("as_of"),
            "items": (_ds["items"] or [])[:8],
        }
    elif isinstance(_ds, list):
        _analysis_ctx["day_summary"] = {"items": _ds[:8]}
    # Stakeholder map — who is engaged, roles, sentiment, risk
    _sm = ai.get("stakeholder_map")
    if isinstance(_sm, dict) and _sm.get("items"):
        _analysis_ctx["stakeholder_map"] = {"items": (_sm["items"] or [])[:10]}
    # EB engagement — the authoritative economic-buyer signal (overrides MEDDPICC EB checkbox)
    _ebe = ai.get("eb_engagement")
    if isinstance(_ebe, dict) and _ebe.get("strength"):
        _analysis_ctx["eb_engagement"] = _ebe
    # Critical signals — the 5-lens what-changed-this-sweep highlights
    _cs = ai.get("critical_signals")
    if isinstance(_cs, list) and _cs:
        _analysis_ctx["critical_signals"] = _cs[:6]
    elif isinstance(_cs, dict) and _cs.get("items"):
        _analysis_ctx["critical_signals"] = (_cs["items"] or [])[:6]
    # Gaps — structured known-unknowns driving the risk read
    _gp = ai.get("gaps")
    if isinstance(_gp, list) and _gp:
        _analysis_ctx["gaps"] = _gp[:5]
    # Champion strength — includes at_risk flag that caps champion rubric factor
    _champ = ai.get("champion_strength")
    if isinstance(_champ, dict):
        _analysis_ctx["champion_strength"] = _champ
    # EB candidates — SF-identified potential economic buyers (title-based, not confirmed)
    _ebc = ai.get("eb_candidates")
    if isinstance(_ebc, list) and _ebc:
        _analysis_ctx["eb_candidates"] = _ebc[:5]
    if _analysis_ctx:
        packet["sweep_analysis"] = _analysis_ctx
    return packet
