"""Deterministic 'is the deal alive?' footprint model.

Reads the buyer's actual footprints — NOT the rep's effort — to decide whether a deal is
alive and kicking relative to its stage, with NO LLM. Sources (all deterministic):
  * Salesforce Tasks — classified by SUBJECT direction (Clari/Outreach sync as TaskSubtype
    'Task', so subtype is unreliable): "Email Received" / "[In]" = BUYER touch;
    "Email Sent" / "[Out]" / "lemlist ... sent" = REP touch.
  * Opportunity summary fields — Last_Email_Received_Date__c, Last_Meeting_Date__c,
    Next_Step_Updated_Date_Time__c, Stage_Changed_Date__c, No_activity_in_last_20_30_Days__c,
    LastActivityDate, Engagement_Score__c.
  * Avoma meeting dates (meeting_cache).

The output feeds Deal Momentum (buyer touch = strong; rep-only = small but non-zero — an
attempt beats silence) and sets days-since to the last BUYER touch. It also serves as the
deterministic fallback when the LLM sweep fails (the deal still shows its real liveness).

Buyer-touch example that broke the LLM sweep: Standard Chartered read "70+ days dark", but
its Tasks show 4 "[Clari - Email Received]" from SCB dated 2026-06-15 — last buyer touch 14d
ago, deal clearly alive.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Expected buyer-cadence by stage (days). A deal is "alive" if the last BUYER footprint is
# within ~1.5x its stage cadence; beyond that it's quiet/at-risk for its stage.
_STAGE_CADENCE = {
    "initial interest": 30, "qualified": 30, "formal evaluation": 21, "evaluation": 21,
    "shortlisted": 18, "vendor selected": 14, "selected": 14,
    "contract in progress": 21, "negotiation": 21, "contracting": 21,
    "contract signed": 30, "po received": 45,
}
_DEFAULT_CADENCE = 30


def _parse_dt(s):
    """Parse an SF date/datetime to a tz-AWARE datetime. Always normalises to UTC so a
    date-only Task ActivityDate (naive) and a timed Event ActivityDateTime (aware) can be
    compared together — mixing the two was crashing derive_footprints with 'can't compare
    offset-naive and offset-aware datetimes', which silently nulled footprints for EVERY
    deal and killed Deal Momentum v2 in prod."""
    if not s:
        return None
    s = str(s)
    dt = None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s[:19] if "T" in s else s[:10], fmt)
                break
            except Exception:  # noqa: BLE001
                continue
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_days(dt, now=None):
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 86400.0)


# Engagement-DEPTH ladder (2026-06-29, user-directed). Each engagement TYPE carries a 0-10
# depth weight = how much buyer commitment it represents. Detected by keyword in a meeting /
# task subject. Highest match wins. These feed Momentum's engagement pillar.
_ENGAGEMENT_WEIGHTS = [
    # (keyword, weight) — order doesn't matter; the max matched weight is used.
    ("proof of concept", 10.0), ("poc", 10.0), ("pilot", 9.0),
    ("roi workshop", 8.0), ("procurement workshop", 8.0), ("workshop", 8.0),
    ("reference call", 7.5), ("reference", 7.0), ("infosec", 7.0), ("security review", 7.0),
    ("info sec", 7.0), ("legal review", 7.0), ("redline", 7.0), ("integration security", 7.0),
    ("face to face", 6.0), ("face-to-face", 6.0), ("f2f", 6.0), ("on-site", 6.0),
    ("onsite", 6.0), ("in-person", 6.0), ("in person", 6.0),
    ("rfp", 6.0), ("rfi", 6.0),
    ("deep dive", 5.0), ("deep-dive", 5.0), ("detailed demo", 5.0), ("technical", 5.0),
    ("tech alignment", 5.0), ("integration", 5.0), ("solution review", 5.0),
    ("demo", 3.0), ("presentation", 3.0), ("walkthrough", 3.0),
    ("discovery", 1.5), ("intro", 1.5), ("kickoff", 2.0), ("kick-off", 2.0),
]


def classify_engagement(subject: str) -> float:
    """Highest engagement-depth weight (0-10) matched in a subject; 0 if nothing matches."""
    s = (subject or "").lower()
    best = 0.0
    for kw, w in _ENGAGEMENT_WEIGHTS:
        if kw in s and w > best:
            best = w
    return best


def _classify(subject: str):
    """-> 'buyer' | 'rep' | None, from a Task subject (direction markers)."""
    s = (subject or "").lower()
    if "email received" in s or "[in]" in s or "[ in ]" in s:
        return "buyer"
    if ("email sent" in s or "[out]" in s or "lemlist" in s
            or "outreach" in s or "invitation" in s and "re:" not in s):
        return "rep"
    if s.startswith("re:") or " re:" in s[:8]:   # a bare "RE:" reply, usually inbound
        return "buyer"
    return None


def _is_email(subject: str, ttype: str = "") -> bool:
    """An email logged in SF (Clari/Outreach sync) — NOT a meeting, regardless of
    keywords in the subject. 'Re: Zycus POC discussion' is an email about a POC, not
    a POC meeting."""
    s = (subject or "").lower() + " " + (ttype or "").lower()
    return ("email sent" in s or "email received" in s or "[clari - email" in s
            or "[clari-email" in s or "[in]" in s or "[out]" in s or "lemlist" in s)


def _meeting_task(subject: str, ttype: str):
    """True only when a record is an ACTUAL meeting — never an email. A loose keyword
    ('poc'/'demo'/'call with') in an EMAIL subject was inflating meetings_60d (Allstate:
    4 'meetings' that were all emails about a POC). Count a meeting only on an explicit
    Meeting type / Clari-Meeting / Avoma marker, or an unambiguous in-person session."""
    if _is_email(subject, ttype):
        return False
    s = (subject or "").lower()
    t = (ttype or "").lower()
    if t == "meeting":
        return True
    return any(w in s for w in ("[clari - meeting]", "[clari-meeting]", "avoma -", "avoma-",
                                "[avoma", "face to face", "face-to-face", " f2f", "onsite",
                                "on-site", "in-person", "workshop", "proof of concept",
                                "deep dive", "deep-dive"))


def _recency_ladder(age_days) -> float:
    """RESCAN_SPEC recency ladder: <=21d=1.0, 22-45=0.85, 46-90=0.65, 91-180=0.45, else 0.25."""
    if age_days is None:
        return 0.5
    if age_days <= 21:
        return 1.0
    if age_days <= 45:
        return 0.85
    if age_days <= 90:
        return 0.65
    if age_days <= 180:
        return 0.45
    return 0.25


def derive_footprints(tasks=None, opp=None, meeting_dates=None, events=None, stage="") -> dict:
    """Pure: compute liveness footprints + the engagement-depth pillar. `tasks`/`events` =
    [{date, subject, type}], `opp` = the SF summary-field dict, `meeting_dates` = [iso str].
    Never raises."""
    tasks = tasks or []
    opp = opp or {}
    now = datetime.now(timezone.utc)

    buyer_dts, rep_dts, meet_dts = [], [], []
    # Engagement events = anything that carries an engagement TYPE (workshop / POC / demo /
    # F2F / themed call). Tasks AND events AND meeting-bearing tasks all qualify.
    eng_events = []  # (eff_weight, raw_weight, subject, age_days)

    # ENGAGEMENT POINTS (2026-07-07 VP spec): Σ(type-weight × who-weight × recency decay) —
    # the momentum engine's primary fuel. TWO clocks are emitted:
    #   points_60d          fast cycle: ≤14d ×1.0, ≤30d ×0.5, ≤60d ×0.2, older ×0
    #   points_90d_process  RFP/process clock (§8.5 v1.1): ≤30d ×1.0, ≤60d ×0.5, ≤90d ×0.2 —
    #                       in a tender, deliverables run 3-6 weeks apart, so a month-old demo
    #                       cluster is RECENT and keeps real weight.
    # A two-way buyer email (no typed keyword) earns 2.0. MASE's own pushed to-dos and generic
    # untyped tasks earn NOTHING (activity theatre).
    _epts = {"v": 0.0, "proc": 0.0}

    def _steep(age):
        if age is None:
            return 0.4
        if age <= 14:
            return 1.0
        if age <= 30:
            return 0.5
        if age <= 60:
            return 0.2
        return 0.0

    def _stretch(age):
        if age is None:
            return 0.4
        if age <= 30:
            return 1.0
        if age <= 60:
            return 0.5
        if age <= 90:
            return 0.2
        return 0.0

    def _ingest(subj, dt, is_buyer_hint=None):
        age = _age_days(dt, now)
        w = classify_engagement(subj)
        if w > 0:
            # buyer-attended/themed engagements keep full weight; a clearly rep-sent email
            # of the same type gets ~40% (an attempt, not a session). Themed/high-tier
            # (>=6, i.e. F2F/RFP/workshop/POC/diligence) are inherently buyer-investment.
            d = _classify(subj)
            rep_only = (d == "rep") and w < 6
            eff = w * _recency_ladder(age) * (0.4 if rep_only else 1.0)
            eng_events.append((round(eff, 2), w, subj, None if age is None else int(age)))
            who = 0.4 if rep_only else 1.0
            _epts["v"] += w * _steep(age) * who
            _epts["proc"] += w * _stretch(age) * who
        else:
            # untyped BUYER touch (a two-way email reply) still counts — weight 2.0
            if _classify(subj) == "buyer":
                _epts["v"] += 2.0 * _steep(age)
                _epts["proc"] += 2.0 * _stretch(age)

    for t in tasks:
        dt = _parse_dt(t.get("date") or t.get("ActivityDate") or t.get("CreatedDate"))
        if dt is None:
            continue
        subj = t.get("subject") or t.get("Subject") or ""
        if _meeting_task(subj, t.get("type") or t.get("Type") or ""):
            meet_dts.append(dt)
        _ingest(subj, dt)
        d = _classify(subj)
        if d == "buyer":
            buyer_dts.append(dt)
        elif d == "rep":
            rep_dts.append(dt)
    for e in (events or []):
        dt = _parse_dt(e.get("date") or e.get("ActivityDateTime") or e.get("CreatedDate"))
        subj = e.get("subject") or e.get("Subject") or ""
        if dt and not _is_email(subj, e.get("type") or e.get("Type") or ""):
            meet_dts.append(dt)   # SF Events are calendar meetings (email-events excluded)
            _ingest(subj, dt)

    # corroborate with SF summary fields + Avoma meetings
    for f in ("Last_Email_Received_Date__c",):
        dt = _parse_dt(opp.get(f))
        if dt:
            buyer_dts.append(dt)
    for f in ("Last_Meeting_Date__c",):
        dt = _parse_dt(opp.get(f))
        if dt:
            meet_dts.append(dt)
    for m in (meeting_dates or []):
        dt = _parse_dt(m)
        if dt:
            meet_dts.append(dt)

    # Dedupe meetings by calendar DATE: the SAME session is routinely logged 3× (an
    # Avoma call + a Clari-synced Event + a "meeting" Task), which was inflating
    # meetings_60d ~9× (Sabic: 55 meetings for 6 real). One meeting-day counts once.
    _md_by_day = {}
    for _dt in meet_dts:
        k = _dt.date()
        if k not in _md_by_day or _dt > _md_by_day[k]:
            _md_by_day[k] = _dt
    meet_dts = list(_md_by_day.values())

    def _latest(dts):
        return max(dts) if dts else None

    last_buyer = _latest(buyer_dts + meet_dts)   # a meeting held IS a buyer touch
    last_meeting = _latest(meet_dts)
    last_rep = _latest(rep_dts)
    # general activity floor from the opp summary (covers deals with no parsed tasks)
    gen = _latest([d for d in (_parse_dt(opp.get("LastActivityDate")),
                               _parse_dt(opp.get("Next_Step_Updated_Date_Time__c"))) if d])

    dsb = _age_days(last_buyer, now)
    cadence = _STAGE_CADENCE.get(str(stage or "").strip().lower(), _DEFAULT_CADENCE)
    # alive: a real BUYER footprint within ~1.5x stage cadence, OR SF says activity in 20-30d.
    no_act = str(opp.get("No_activity_in_last_20_30_Days__c") or "").strip()
    sf_active = no_act in ("0", "false", "False", "no", "No")
    alive = bool((dsb is not None and dsb <= 1.5 * cadence) or sf_active)

    # --- Engagement pillar: the highest-tier RECENT engagement sets the floor; multiple
    # recent engagements add a frequency bump. Only the last 60 days count. ---
    recent = [e for e in eng_events if (e[3] is None or e[3] <= 60)]
    top_eff = max((e[0] for e in recent), default=0.0)
    top = max(recent, key=lambda e: e[0], default=None)
    # Count engagement FREQUENCY by distinct days (not raw rows) so the same session
    # logged across Avoma/Clari/Task doesn't inflate it (Sabic events_30d 40 -> ~real).
    n30 = len({e[3] for e in eng_events if e[3] is not None and e[3] <= 30 and e[1] >= 3})

    out = {
        "last_buyer_touch": last_buyer.date().isoformat() if last_buyer else None,
        "last_meeting": last_meeting.date().isoformat() if last_meeting else None,
        "last_rep_touch": last_rep.date().isoformat() if last_rep else None,
        "days_since_buyer_touch": None if dsb is None else int(dsb),
        "buyer_touches_30d": sum(1 for d in buyer_dts if (_age_days(d, now) or 999) <= 30),
        "buyer_touches_60d": sum(1 for d in buyer_dts if (_age_days(d, now) or 999) <= 60),
        "meetings_60d": sum(1 for d in meet_dts if (_age_days(d, now) or 999) <= 60),
        "rep_only": bool(rep_dts and not buyer_dts and not meet_dts),
        "stage_cadence_days": cadence,
        "alive": alive,
        "general_last_activity": gen.date().isoformat() if gen else None,
        "engagement": {
            "top_weight": round(top_eff, 2),          # recency-weighted depth of the best recent engagement (0-10)
            "top_event": top[2] if top else None,
            "raw_top": top[1] if top else 0,           # un-decayed depth (e.g. 8 = workshop)
            "events_30d": n30,                         # count of real (>=demo) engagements in 30d
            "points_60d": round(min(_epts["v"], 60.0), 1),  # Σ(type × who × steep decay) — momentum fuel
            "points_90d_process": round(min(_epts["proc"], 60.0), 1),  # §8.5 stretched RFP clock
        },
    }
    return out


def footprint_momentum_signals(fp: dict) -> dict:
    """Map footprints -> momentum signal strengths [0,1] (buyer strong, rep small).
    Consumed by score_momentum via ai.momentum_signals-style keys."""
    fp = fp or {}
    sig = {}
    dsb = fp.get("days_since_buyer_touch")
    cad = fp.get("stage_cadence_days") or _DEFAULT_CADENCE
    if dsb is not None and dsb <= cad:
        sig["buyer_engaged_this_sweep"] = round(max(0.4, 1.0 - dsb / (2.0 * cad)), 3)
    if (fp.get("meetings_60d") or 0) > 0:
        sig["meeting_held_recently"] = 0.7
    if (fp.get("buyer_touches_60d") or 0) >= 2:
        sig["customer_action_items_increasing"] = 0.5
    if fp.get("rep_only"):
        sig["rep_attempt_only"] = 0.15        # small credit: an attempt beats silence
    return sig
