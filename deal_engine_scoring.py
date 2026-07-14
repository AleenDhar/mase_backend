"""
Deterministic per-opportunity deal scoring, computed INSIDE the sweep.

Produces five scores + a read label per opportunity, each with a short
plain-English commentary, written onto the canonical record at
`ai.deal_scores`. This module does NO interpretation and makes NO LLM calls —
it is arithmetic over signals the sweep already derived (pulse, north-star
verdict, MEDDPICC statuses, competitive position, evidence coverage, stakeholder
map, durable packets), so the same swept record always yields the same scores.

  PRIMARY (each 0-100)
    win_position         can we win it
    deal_momentum        forward / flat / backward (50 = flat)
    customer_commitment  how much the customer has actually invested
    deal_risk            observed reasons the deal could break
  DERIVED
    forecast_confidence  roll-up, attenuated by read quality
    evidence_coverage    read label (Full / Solid / Partial / Early)

Design rules (kept identical to the offline model so scores reconcile):
  * Three evidence states: positive, negative, unobserved. Unobserved contributes
    nothing — absence is never a penalty on a primary score, only on confidence.
  * Baseline + bonus-only Lift for Win; Momentum eases toward 50 on silence;
    Commitment is earned from a floor; Risk is observed-only.
  * HYBRID factor source: factors are DERIVED from the swept record here; if the
    sweep agent additionally emitted `ai.deal_scores_evidence.factors` (the soft
    judgment factors), those are overlaid on top of the derived ones.

Never raises: compute_deal_scores() is wrapped so a malformed record degrades to
an empty result and the sweep continues.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from math import exp
from typing import Optional

ENABLED = os.getenv("DEAL_SCORES_ENABLED", "1").strip().lower() not in ("0", "false", "no", "")
SCHEMA_VERSION = 1

# Dated milestones embedded in a running Next_Step log (ISO, m/d, or "Jul 24"). Counting
# distinct dates is our proxy for "the next step is actively worked with real milestones"
# — Salesforce Next_Step__c history-tracking is off, so true update-cadence isn't available.
_DATE_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2})\b", re.I)


def _count_dated_milestones(text) -> int:
    return len(set(m.lower() for m in _DATE_RE.findall(str(text or ""))))


@dataclass
class Signal:
    strength: float
    evidence: str = ""


# ----------------------------------------------------------------------------
# Weight tables (points of swing at full strength) — identical to offline model
# ----------------------------------------------------------------------------
WIN_BASELINE = {"pain_fit": 12, "engagement_direction": 10,
                "stage_evidence_alignment": 8, "competitive_posture": 10}
WIN_LIFT = {"exec_access": 10, "champion_strength": 8, "commercial_motion": 8,
            "customer_action_items": 6, "stakeholder_expansion": 5}
WIN_LIFT_CAP, WIN_LIFT_SCALE = 30.0, 18.0

MOMENTUM = {
    "seniority_rising": (+1, 9), "customer_action_items_increasing": (+1, 9),
    "commercial_topics_entering": (+1, 8), "concrete_dates": (+1, 6),
    "customer_requested_next_meeting": (+1, 8), "close_plan_concretizing": (+1, 7),
    "stage_advanced_with_evidence": (+1, 8), "close_date_pushed": (-1, 9),
    "stage_stuck_past_cadence": (-1, 7), "customer_passivity": (-1, 8),
    "attendance_or_cadence_drop": (-1, 7), "generic_demo_only": (-1, 5),
    "competitor_praised": (-1, 6), "buyer_engaged_this_sweep": (+1, 10),
    # A close date pulled FORWARD is a strong forward-momentum signal (the buyer is
    # accelerating); frequent dated Next-Step milestones show the deal is being worked.
    "close_date_pulled_forward": (+1, 8), "next_step_active": (+1, 6),
}
MOMENTUM_DECAY_TAU = 14.0
MOMENTUM_STALL_MAX = 25.0   # a deal far past cadence loses up to this much momentum (sinks below 50)
# Momentum is read over a BROADER 30-60 day window (2026-06-29, user-directed): a deal is
# only "stalling" once it has been quiet beyond ~30 days, and the drag scales across the
# next 30 (tau 30 -> meaningful by 60d). Separate from MOMENTUM_DECAY_TAU (coverage recency).
MOMENTUM_WINDOW = 30.0
MOMENTUM_STALL_TAU = 30.0

COMMITMENT = {"customer_action_items": 10, "internal_process_shared": 10,
              "exec_access_granted": 9, "customer_next_meeting_request": 7,
              "security_or_procurement_review": 9, "deep_eval_or_reference_request": 8}
COMMITMENT_FLOOR, COMMITMENT_SCALE = 8.0, 26.0

RISK = {"close_date_pushed_repeatedly": 14, "stage_inflation": 14,
        "competitor_preferred": 13, "open_competitive_rfp": 9, "customer_passivity": 11,
        "low_buyer_intent": 11, "next_meeting_declined": 10, "budget_frozen_or_unclear": 9,
        "access_blocked": 12}
RISK_SCALE = 30.0

FC_WEIGHTS = {"win": 0.30, "momentum": 0.20, "commitment": 0.25, "risk_inverse": 0.25}
FC_COVERAGE_FLOOR = 0.50

READ_DIMENSIONS = {
    "pain_and_fit": ["pain_fit"],
    "engagement": ["engagement_direction", "customer_action_items", "customer_passivity", "low_buyer_intent"],
    "stage_integrity": ["stage_evidence_alignment", "stage_inflation", "stage_advanced_with_evidence", "stage_stuck_past_cadence"],
    "competitive": ["competitive_posture", "competitor_preferred", "competitor_praised", "open_competitive_rfp"],
    "exec_and_champion": ["exec_access", "champion_strength", "exec_access_granted", "access_blocked", "seniority_rising", "stakeholder_expansion"],
    "commercial": ["commercial_motion", "internal_process_shared", "security_or_procurement_review", "budget_frozen_or_unclear", "close_plan_concretizing", "deep_eval_or_reference_request"],
    "forward_motion": ["customer_requested_next_meeting", "customer_next_meeting_request", "next_meeting_declined", "concrete_dates", "close_date_pushed", "close_date_pulled_forward", "next_step_active", "customer_action_items_increasing", "commercial_topics_entering"],
}
COVERAGE_BANDS = [(90, "Full Read"), (75, "Solid Read"), (50, "Partial Read"), (0, "Early Read")]

SIGNED_KEYS = set(WIN_BASELINE)
MAGNITUDE_KEYS = set(WIN_LIFT) | set(MOMENTUM) | set(COMMITMENT) | set(RISK)
ALL_KEYS = SIGNED_KEYS | MAGNITUDE_KEYS

# Recency ladder applied to dynamic factor strengths (days since last real touch).
def _recency_weight(days: Optional[int], structural: bool = False) -> float:
    if days is None:
        return 1.0
    if days <= 21:
        w = 1.0
    elif days <= 45:
        w = 0.85
    elif days <= 90:
        w = 0.65
    elif days <= 180:
        w = 0.45
    else:
        w = 0.25
    return max(w, 0.60) if structural else w


# ----------------------------------------------------------------------------
# Arithmetic (identical to offline model)
# ----------------------------------------------------------------------------
def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _saturate(raw, cap, scale):
    return 0.0 if raw <= 0 else cap * (1.0 - exp(-raw / scale))


def _get(ev, key):
    s = ev.get(key)
    return s if isinstance(s, Signal) else None


def _contrib(label, points, evidence):
    return {"factor": label, "points": round(points, 1), "evidence": evidence}


# --- Stage-anchored win probability -------------------------------------------------
# Win = a STAGE PRIOR (how far through buying = how much is left to close) + a bounded
# +/-15 adjustment from within-stage signals. Calibrated 2026-06-29 (user-approved
# "standard enterprise" anchors + Anchored +/-15). The old flat-50 baseline made win
# nearly flat across stages (Qualified 62 -> Contract 72); this makes the stage drive it.
WIN_STAGE_ANCHOR = [           # (substring, prior) — checked in order, most specific first
    ("po received", 98), ("po-received", 98),
    ("contract signed", 95), ("closed won", 99), ("won", 97),
    ("contract", 85), ("negotiat", 85),
    ("vendor select", 72), ("selected", 72),
    ("shortlist", 55),
    ("formal eval", 35), ("evaluation", 35),
    ("qualif", 18),
    ("initial interest", 8), ("interest", 8),
]
WIN_ANCHOR_DEFAULT = 35.0
# Stage CEILINGS on Win (2026-06-29, user-directed): you cannot be highly confident of
# winning until the buyer is structurally committed to the round / has selected you.
#   BEFORE RFP (Initial Interest, Qualified)            -> max 30
#   DURING RFP (Formal Evaluation, Shortlisted)         -> max 70
#   POST-shortlist (Vendor Selected -> Contract -> PO)  -> up to 100
WIN_STAGE_CEILING = [          # (substring, ceiling) — most specific first
    ("po received", 100), ("po-received", 100),
    ("contract signed", 100), ("won", 100),
    ("contract", 100), ("negotiat", 100),
    ("vendor select", 100), ("selected", 100),
    ("shortlist", 70),
    ("formal eval", 70), ("evaluation", 70),
    ("qualif", 30),
    ("initial interest", 30), ("interest", 30),
]
WIN_CEILING_DEFAULT = 70.0


def _win_ceiling(record: dict) -> float:
    s = str(((record or {}).get("hard") or {}).get("stage") or "").lower()
    for sub, cap in WIN_STAGE_CEILING:
        if sub in s:
            return float(cap)
    return WIN_CEILING_DEFAULT


# Stage EXPECTED momentum: later stages should be MORE active (Vendor Selected should be hot
# with contracting motion). When a deal's momentum falls below its stage expectation, Win is
# dragged DOWN — the anchor falls fast if the stage's expected motion isn't happening
# (2026-06-29, user-directed: drastic x1.0, no floor). Signed/PO ease back (quiet legal = ok).
WIN_EXPECTED_MOMENTUM = [       # (substring, expected) — most specific first
    ("po received", 55), ("po-received", 55),
    ("contract signed", 55), ("won", 55),
    ("contract", 62), ("negotiat", 62),
    ("vendor select", 60), ("selected", 60),
    ("shortlist", 56),
    ("formal eval", 52), ("evaluation", 52),
    ("qualif", 50),
    ("initial interest", 48), ("interest", 48),
]
# Momentum feeds Win BIDIRECTIONALLY (2026-06-29): below stage-expected momentum CHIPS Win off
# (drastic, x1.0, no floor); above-expected ADDS muscle (modest, x0.5). The ceiling still caps.
WIN_MOMENTUM_DOWN_RATE = 1.0    # Win lost per point momentum is BELOW stage-expected
WIN_MOMENTUM_UP_RATE = 0.5      # Win gained per point momentum is ABOVE stage-expected


def _expected_momentum(record: dict) -> float:
    s = str(((record or {}).get("hard") or {}).get("stage") or "").lower()
    for sub, exp in WIN_EXPECTED_MOMENTUM:
        if sub in s:
            return float(exp)
    return 52.0
# RUBRIC WIN (2026-06-29, user-directed): keep the STAGE ANCHOR as the base, then apply a
# signed adjustment of up to +/-WIN_RUBRIC_BAND driven by the FULL rubric factor table.
# Strong rubric evidence ADDS to the stage base; weak/negative evidence CHIPS OFF; MISSING
# evidence is treated as a MILD NEGATIVE ("we haven't proven it yet"). Weights are the
# rubric's out-of-100 weights.
WIN_RUBRIC_BAND = 30.0
RUBRIC_WIN_WEIGHTS = {        # rubric table, sums to 100
    "differentiation": 20,    # Zycus differentiation fit (AI/S2P/intake/sourcing/AP/contracts…)
    "preference": 20,         # customer preference for Zycus (says positive, compares favourably)
    "champion": 15,           # champion strength
    "exec_access": 15,        # CPO/CFO/CIO/VP procurement involved
    "competitive": 15,        # ahead / equal / behind named rivals
    "business_case": 10,      # ROI / savings / automation / compliance quantified
    "commercial": 5,          # pricing / scope / timeline acceptable
}
WIN_MISSING = -0.30           # unknown evidence = mild negative (chip off the base)


def _win_anchor(record: dict) -> float:
    s = str(((record or {}).get("hard") or {}).get("stage") or "").lower()
    for sub, prior in WIN_STAGE_ANCHOR:
        if sub in s:
            return float(prior)
    return WIN_ANCHOR_DEFAULT


def _status_strength(status, *, present=1.0, partial=0.3, gap=-0.5, missing=WIN_MISSING) -> float:
    """Map a MEDDPICC-style status string to a signed [-1,1] strength."""
    s = str(status or "").strip().lower()
    if not s:
        return missing
    if s in ("confirmed", "present", "strong", "yes", "high", "engaged", "identified", "complete"):
        return present
    if s in ("partial", "developing", "medium", "moderate", "some", "in_progress", "emerging"):
        return partial
    if s in ("gap", "none", "no", "low", "missing", "unknown", "not identified", "unmapped",
             "weak", "at_risk", "at risk"):
        return gap
    return missing


# A competitor only drags the WIN score when the BUYER is leaning toward them (a
# preference / buying signal) — NOT merely because a credible rival is present in the
# evaluation. `threat_level` measures how dangerous a competitor COULD be, not whether
# the buyer prefers them, so it must never by itself trigger the competitive win
# penalty (2026-07-03, user-directed). These status values ARE a buyer-leaning signal.
_COMPETITOR_LEANING = (
    "preferred", "ahead", "incumbent", "winning", "leading", "selected",
    "frontrunner", "front-runner", "front runner", "favored", "favoured",
    "chosen", "recommended", "shortlist leader", "down-selected to")


def _buyer_leans_competitor(c: dict) -> bool:
    """True only when a competitor entry shows the BUYER leaning toward them — a real
    preference / down-select / buying signal in `status` (or an explicit `preferred`
    flag / a leaning phrase in `sentiment`). A merely high `threat_level` does NOT
    count: a strong rival that is present but not preferred is not a losing signal."""
    if not isinstance(c, dict):
        return False
    if c.get("preferred") is True or c.get("buyer_leaning") is True:
        return True
    status = str(c.get("status") or "").lower()
    sentiment = str(c.get("sentiment") or "").lower()
    # A competitor being DISPLACED — negative sentiment, or a declined/faded status — is a
    # WIN signal, never a buyer lean. In particular an INCUMBENT we're replacing must not drag
    # Win (Bright Horizons: the displaced incumbent Proactis wrongly scored as buyer-leaning).
    displaced = (any(w in sentiment for w in ("negative", "declin", "displac", "replac", "lost"))
                 or any(w in status for w in ("declined", "faded", "do_nothing", "do nothing")))
    for f in ("status", "buyer_preference", "sentiment"):
        v = str(c.get(f) or "").lower()
        for t in _COMPETITOR_LEANING:
            if t in v:
                if t == "incumbent" and displaced:
                    continue   # a displaced incumbent is not a buyer lean
                return True
    return False


def _competitive_strength(ai: dict) -> float:
    """+ when Zycus is the only real option (do-nothing rival), STRONG - only when the
    BUYER is leaning toward a real vendor, roughly even when credible rivals are merely
    present, mild-negative when unknown."""
    comp = ai.get("competitive_position") or {}
    items = comp.get("competitors") or comp.get("items") or []
    real_leaning = False
    has_real = False
    for c in (items if isinstance(items, list) else []):
        nm = str(c.get("name") or "").lower()
        if any(t in nm for t in ("do nothing", "do-nothing", "manual", "status quo", "in-house", "inertia")):
            continue  # not a vendor threat
        has_real = True
        if _buyer_leans_competitor(c):
            real_leaning = True
    if real_leaning:
        return -1.0          # buyer is leaning toward a competitor -> real win hit
    if items and not has_real:
        return 0.5            # only do-nothing / manual rival -> we're the wedge
    if has_real:
        return 0.2            # credible rivals present but no leaning -> roughly even
    # SOLE-SOURCE (VP spec §6): no competitor on record because the buyer SKIPPED the RFP and
    # came direct is a STRENGTH, not an unknown. Only fires on explicit evidence in the
    # competitive summary — a mere data gap still reads mild-negative below.
    _cs_txt = (str(comp.get("summary") or "") + " " + str(comp.get("position") or "")).lower()
    if re.search(r"sole[- ]?sourc|no rfp|skipped\s+(?:an?\s+)?rfp|came\s+(?:to\s+us\s+)?direct|single[- ]vendor|only vendor invited", _cs_txt):
        return 0.5
    return _status_strength((ai.get("meddpicc") or {}).get("competition", {}).get("status"),
                            present=0.3, partial=0.1)


def _rubric_win_strengths(record: dict) -> dict:
    """Signed [-1,1] strength per rubric factor, read from the swept record. Sweep-emitted
    fields (customer_preference, business_case) are used when present; otherwise mapped from
    the best available structured evidence; absent -> mild negative."""
    ai = record.get("ai") or {}
    medd = ai.get("meddpicc") or {}
    mst = lambda k: (medd.get(k) or {}).get("status")
    out = {}

    # Differentiation fit — AI-fit tier, else pain identified. The tier vocabulary is
    # MASE's AI-appetite ladder (AI Hungry / AI Curious / AI Cool), NOT just "high/med/low"
    # — an unmapped 'AI Hungry' was falling to the -0.4 default, reading a buyer RACING toward
    # AI as "losing on capability fit" (Global Switch). Map the real vocabulary; an UNKNOWN
    # tier falls back to the pain signal, never a blind negative.
    tier = str((ai.get("ai_fit_signal") or {}).get("tier") or "").lower()
    if tier:
        if any(t in tier for t in ("high", "strong", "excellent", "a+", "tier 1", "tier1",
                                   "hungry", "racing", "leading", "advanced", "eager", "champion")):
            out["differentiation"] = 1.0
        elif any(t in tier for t in ("med", "moderate", "b", "tier 2", "curious", "interested",
                                     "warm", "exploring", "growing", "open")):
            out["differentiation"] = 0.3
        elif any(t in tier for t in ("cool", "cold", "skeptical", "resistant", "laggard",
                                     "reluctant", "tier 3", "tier3", " low", "low ", "none")):
            out["differentiation"] = -0.4
        else:
            out["differentiation"] = _status_strength(mst("identify_pain"))
    else:
        out["differentiation"] = _status_strength(mst("identify_pain"))

    # Customer preference for Zycus — sweep field if present, else positioning proxy.
    pref = ai.get("customer_preference")
    if isinstance(pref, dict) and (pref.get("level") or pref.get("status")):
        out["preference"] = _status_strength(pref.get("level") or pref.get("status"),
                                             present=1.0, partial=0.4)
    else:
        pos = ai.get("ai_positioning_strength") or {}
        sc = pos.get("score")
        if pos.get("under_positioned") is True:
            out["preference"] = -0.4
        elif isinstance(sc, (int, float)):
            out["preference"] = max(-0.5, min(1.0, (sc - 50.0) / 50.0 if sc > 1 else sc * 2 - 1))
        elif isinstance(sc, str) and sc.strip():
            # the sweep sometimes writes positioning as PROSE ('Moderate-High interest,
            # maturity gap flagged') not a number — parse the sentiment instead of defaulting
            # negative (Bosch read 'leaning elsewhere' on a Moderate-High-interest deal).
            _s = sc.lower()
            if any(t in _s for t in ("very high", "strong", "high interest", "moderate-high", "moderate to high", "preferred", "leading", "front-runner", "well positioned", "landing strongly")):
                out["preference"] = 0.5
            elif any(t in _s for t in ("moderate", "medium", "warm", "positive", "good interest", "interested")):
                out["preference"] = 0.3
            elif any(t in _s for t in ("low", "weak", "under", "behind", "poor", "cool", "negative")):
                out["preference"] = -0.3
            else:
                out["preference"] = WIN_MISSING
        else:
            out["preference"] = WIN_MISSING

    # Champion strength — explicit strength label, else MEDDPICC champion status.
    cs = str((ai.get("champion_strength") or {}).get("strength") or "").lower()
    out["champion"] = (_status_strength(cs, present=1.0, partial=0.3) if cs
                       else _status_strength(mst("champion")))

    # Exec access reads the economic_buyer status AFTER the hard-flag floor — a no-evidence
    # 'partial' that contradicts SF's eb_identified=False must not hand out exec-access credit.
    out["exec_access"] = _status_strength(_eb_status_floored(record))
    # SECOND-PANEL / expansion into a WON account (user-directed, Fortive): if Zycus already
    # closed a deal on this account (the sweep flags ai.expansion_context.prior_closed_won —
    # e.g. a sibling Closed-Won opp), we ALREADY hold executive / seat / stakeholder access.
    # Floor exec_access so a not-yet-mapped EB on the expansion opp can't read as "no access".
    exp = ai.get("expansion_context")
    if isinstance(exp, dict) and exp.get("prior_closed_won"):
        out["exec_access"] = max(out.get("exec_access", WIN_MISSING), 0.6)
    out["competitive"] = _competitive_strength(ai)
    # Business case — sweep field if present, else MEDDPICC metrics.
    bc = ai.get("business_case")
    out["business_case"] = (_status_strength((bc or {}).get("status") or (bc or {}).get("level"))
                            if isinstance(bc, dict) and (bc.get("status") or bc.get("level"))
                            else _status_strength(mst("metrics")))
    out["commercial"] = _status_strength(mst("paper_process"), present=1.0, partial=0.4)
    # BROADEN THE SOURCE (2026-06-29, user-directed): overlay deterministic CRM evidence the
    # sweep stored from MEDDPICC 2.0 / Next-Step / completed Tasks. Take the BEST evidence
    # across the LLM read and the raw CRM (so a named EB in MEDDPICC 2.0 lifts exec_access even
    # if the LLM under-read it), recency-weighted (recent evidence counts for more).
    out = _crm_evidence_overlay(out, ai)

    # HONEST-EXAMINATION GUARDS (2026-07-08, user-directed): the rubric was reading MAX strength off
    # prose keywords while the deal's OWN flags said otherwise — the "going easy on deals" inflation.
    # 1) An AT-RISK champion is NOT a full-strength champion. Austrian Post: champion_strength.at_risk
    #    = true (the champion himself said they're being "strung along") + MEDDPICC champion = partial,
    #    yet the prose label scored +1.0. Cap an at-risk champion at partial.
    if (ai.get("champion_strength") or {}).get("at_risk"):
        out["champion"] = min(out.get("champion", 0.0), 0.3)
    # 4) EB ENGAGEMENT GUARD (2026-07-14): eb_engagement.strength is the call-evidence read on
    #    exec access. When the sweep emitted it, _eb_status_floored() already used it as the
    #    primary signal. Here we apply the symmetric cap:
    #    field_only/none → exec_access must not exceed partial (0.3); the CRM overlay
    #    (_crm_evidence_overlay) may have re-lifted it to 0.7 via the MAX-merge — undo that.
    #    direct/indirect → floor exec_access UP so the overlay can't accidentally deflate it.
    _eb_eng_str = str((ai.get("eb_engagement") or {}).get("strength") or "").strip().lower()
    if _eb_eng_str in ("field_only", "none"):
        out["exec_access"] = min(out.get("exec_access", WIN_MISSING), 0.3)
    elif _eb_eng_str == "indirect":
        out["exec_access"] = max(out.get("exec_access", WIN_MISSING), 0.3)
    elif _eb_eng_str == "direct":
        out["exec_access"] = max(out.get("exec_access", WIN_MISSING), 0.7)
    # 2) MAX preference off a NARRATIVE KEYWORD, with NO structured customer_preference AND the deal
    #    visibly DECLINING (forecast cut / amount cut), is an over-read — a technically-ahead but
    #    cost-squeezed, cautious buyer is not "maximum preference". Cap at moderate. (A genuine
    #    structured high preference, or a non-declining deal, is untouched.)
    _cp = ai.get("customer_preference") or {}
    _ot = ai.get("opp_trends") or {}
    _declining = ((isinstance(_ot.get("forecast_category_trend"), (int, float)) and _ot["forecast_category_trend"] < -0.02)
                  or (isinstance(_ot.get("amount_trend"), (int, float)) and _ot["amount_trend"] < -0.2))
    if not (_cp.get("level") or _cp.get("status")) and _declining and out.get("preference", 0.0) > 0.5:
        out["preference"] = 0.5
    # 3) BLOCKED DIFFERENTIATOR (2026-07-08): when the deal's OWN blockers explicitly negate the AI
    #    story ("ZERO AI functionality will be accepted", "no AI permitted" — data-residency /
    #    security bans), differentiation cannot read MAX off that same AI story (SAMI: differentiation
    #    +1.0 from the Merlin narrative while a MEDDPICC blocker said ZERO AI will be accepted).
    #    Capped at moderate — the fit interest is real, the differentiator is blocked.
    if out.get("differentiation", 0.0) > 0.3:
        _blk = " ".join(str(v.get("detail") or "") for v in ((ai.get("vulnerabilities") or {}).get("items") or [])
                        if isinstance(v, dict))
        _blk += " " + str((record.get("hard") or {}).get("blockers") or "") + " " + str(ai.get("blockers") or "")
        if re.search(r"zero\s+ai|no\s+ai\s+(?:functionality|features?|capabilit)|ai\s+(?:functionality\s+)?"
                     r"(?:will\s+not|won'?t|cannot|can'?t)\s+be\s+accepted|ai\s+(?:is\s+)?"
                     r"(?:banned|prohibited|not\s+(?:permitted|allowed|accepted))", _blk, re.I):
            out["differentiation"] = 0.3
    return out


# Factors the deterministic CRM/Next-Step overlay can LIFT (presence = favourable). Includes
# `preference` (playbook weight-20 factor) — it has no MEDDPICC field, so before the Next-Step/
# narrative keyword scan it could only ever read as "missing" (-0.30) and silently capped Win.
_CRM_FACTOR_KEYS = ("differentiation", "preference", "champion", "exec_access",
                    "business_case", "commercial")
# The soft, keyword-prone factors: a Next-Step/narrative keyword hit must NOT override an
# EXPLICIT weak/negative read the sweep wrote (champion "weak", preference "leaning
# elsewhere", "differentiation" off-fit). Without this a bare keyword maxed the factor to
# +1 while the narrative said the opposite — the score-vs-reasons mismatch (SARS). The
# structured-field factors (exec_access from a named EB, business_case from metrics) still
# lift, because those are real evidence, not a keyword.
_OVERLAY_LOCK_IF_NEGATIVE = ("preference", "differentiation", "champion", "commercial")
_EXPLICIT_NEGATIVE = -0.4   # below the -0.30 "unknown" floor: an explicit gap/weak/at-risk read


def _crm_recency(age_days) -> float:
    if age_days is None:
        return 0.7
    if age_days <= 30:
        return 1.0
    if age_days <= 90:
        return 0.85
    if age_days <= 180:
        return 0.6
    return 0.4


def _crm_evidence_overlay(out: dict, ai: dict) -> dict:
    """Lift 'presence = good' factor strengths from ai.crm_evidence (deterministic, multi-
    source). Only ever HELPS (max), so a CRM-confirmed factor can't be hidden by an LLM miss.
    Competition is excluded (a named competitor isn't necessarily favourable).

    Two guard layers:
    1. _OVERLAY_LOCK_IF_NEGATIVE: CRM can't override an explicit negative AI read (cur ≤ -0.4).
    2. _AI_ASSESSED_FACTORS: for factors where the AI has a dedicated call-evidence field
       (differentiation from ai_fit_signal, business_case from ai.business_case, commercial
       from medd.paper_process), CRM can only lift from the "missing" floor (-0.30). If the AI
       has already assessed the factor at any level above missing, that assessment is the ceiling
       — a rep-entered MEDDPICC field cannot inflate above a genuine AI read.
       This closes the symmetric gap to the champion/eb guards: "AI Curious" stays 0.3, not 0.7.
    """
    ev = ai.get("crm_evidence")
    if not isinstance(ev, dict):
        return out

    # Factors where the AI already has a dedicated call-evidence assessment.
    # CRM may lift from the "missing" floor (-0.30) but never beyond the AI's own read.
    _AI_ASSESSED_FACTORS = frozenset({"differentiation", "business_case", "commercial"})

    for fac in _CRM_FACTOR_KEYS:
        info = ev.get(fac)
        if isinstance(info, dict) and info.get("present"):
            cur = out.get(fac, WIN_MISSING)
            # Guard 1: don't override explicit negative reads for keyword-prone factors.
            if fac in _OVERLAY_LOCK_IF_NEGATIVE and cur <= _EXPLICIT_NEGATIVE:
                continue
            s = round(1.0 * _crm_recency(info.get("age_days")), 3)
            # Guard 2: if AI already assessed this factor, CRM can't inflate above that read.
            if fac in _AI_ASSESSED_FACTORS and cur > WIN_MISSING:
                s = min(s, cur)
            out[fac] = max(cur, s)
    return out


# Opportunity-trend signals (from field history) nudge Win within its band: stage/forecast
# moves weigh a bit more than amount/close. TREND_INFLUENCE keeps them MODEST relative to the
# rubric (a fully-positive trend set shifts Win ~TREND_INFLUENCE x BAND).
WIN_TREND_WEIGHTS = {"stage_trend": 1.0, "forecast_category_trend": 1.0,
                     "amount_trend": 0.7, "close_date_trend": 0.7}
WIN_TREND_INFLUENCE = 0.40


def _opp_trend_net(record: dict):
    """Weighted-average signed trend in [-1,1] from ai.opp_trends; None if no trends."""
    trends = ((record or {}).get("ai") or {}).get("opp_trends") or {}
    if not isinstance(trends, dict):
        return None, []
    num = den = 0.0
    detail = []
    for k, w in WIN_TREND_WEIGHTS.items():
        v = trends.get(k)
        if isinstance(v, (int, float)):
            num += w * float(v)
            den += w
            detail.append((k, float(v)))
    if den == 0:
        return None, []
    return max(-1.0, min(1.0, num / den)), detail


# --- Deal Momentum v2: PURELY engagement + next-steps + new milestones (user-directed) ----
# Centered on 50. The dominant pillar is engagement DEPTH (a recent POC ≫ a standard demo),
# scaled up so engagement drives the score, then next-step freshness + new milestones, minus
# an asymmetric stall drag. Reads ai.footprints (engagement + buyer recency) which the sweep
# computes from SF Events/Tasks. Falls back to the signal-based score_momentum when absent.
MOM_ENG_SCALE = 2.6           # engagement points = top_weight(0-10) * this  -> POC 26, workshop 21
MOM_ENG_CAP = 30.0
MOM_NEXTSTEP_CAP = 8.0
MOM_MILESTONE_CAP = 8.0
MOM_STALL_CAP = 28.0


def _days_since(iso):
    if not iso:
        return None
    try:
        from datetime import datetime, timezone
        d = datetime.fromisoformat(str(iso)[:10])
        return (datetime.now(timezone.utc).replace(tzinfo=None) - d).days
    except Exception:  # noqa: BLE001
        return None


# --- Deal Momentum v3 (2026-07-07 spec §02): DIRECTION, not activity ------------------------
# A busy next-step log on a slipping deal is still slipping. PRIMARY signals decide the score —
# close-date direction (most reliable), genuine buyer-touch recency, confidence-% trajectory.
# Engagement/milestones shape it within that read (REAL sessions only). Next-step edit
# frequency + cumulative dated-line count NEVER raise it; the false-velocity rule holds a
# busy-but-slipping deal low. Centered on 50.
MOM_CLOSE_WEIGHT = 18.0            # close-date direction ±18 (symmetric, primary)
_CONF_RE = re.compile(r"confidence\D{0,8}(\d{1,3})\s*%", re.I)
# A real 'top event' is a short session label (demo/workshop/POC/call/meeting); a long prose
# sentence or competitive-analysis note masquerading as an event is NOT a session (spec guard).
_NARRATIVE_HINT = re.compile(r"\b(will open|competitive field|the only named|incumbent|motivated to switch|"
                             r"rfp will|expected to|analysis|assessment|likely|strategy|positioning)\b", re.I)


def _parse_confidence(text):
    """Most-recent 'Confidence NN%' in the (newest-first) Next-Step log; None if absent."""
    m = _CONF_RE.search(str(text or ""))
    if not m:
        return None
    try:
        v = int(m.group(1))
        return v if 0 <= v <= 100 else None
    except ValueError:
        return None


def _is_real_session(top_event) -> bool:
    t = str(top_event or "").strip()
    return bool(t) and len(t) <= 90 and not _NARRATIVE_HINT.search(t)


# --- RELATIONSHIP LEVERAGE (2026-07-07 user spec): an EXPANSION / PHASE-2 / ADD-ON opp on an
# account we already run (a sibling Closed-Won, or a strong live sibling deal) inherits the
# rapport — it must not read cold just because its own record is young/thin.
#   WIN: +10 relationship points when the account has a sibling Closed-Won OR a strong live
#        sibling (win >= 60 or momentum >= 60). Stage ceilings still apply.
#   MOMENTUM: the strongest sibling's momentum partially wraps on (35% of the gap, cap +12).
# Reads ai.account_context {sibling_closed_won, best_sibling_win, best_sibling_mom,
# sibling_name} — stamped by the rescore pass / sweep from the same-account sibling index —
# with ai.expansion_context.prior_closed_won as the legacy signal.
REL_WIN_PTS = 10.0
REL_MOM_FRACTION = 0.35
REL_MOM_CAP = 12.0


def _relationship_context(record: dict):
    """(qualifies: bool, why: str, best_sibling_mom: float|None)"""
    ai = (record or {}).get("ai") or {}
    rel = ai.get("account_context") if isinstance(ai.get("account_context"), dict) else {}
    prior_won = bool(rel.get("sibling_closed_won")
                     or (isinstance(ai.get("expansion_context"), dict)
                         and ai["expansion_context"].get("prior_closed_won")))
    bw = _num(rel.get("best_sibling_win")) or 0.0
    bm = _num(rel.get("best_sibling_mom")) or 0.0
    nm = str(rel.get("sibling_name") or "a sibling deal")
    if prior_won:
        return True, f"existing account relationship — Zycus already closed-won on this account; rapport carries to this expansion", bm or None
    # A strong LIVE sibling counts even when its OWN win is capped by qualification (a Shortlisted /
    # Best-Case sibling reads ~52 under the Access-to-Power gate but is a real foothold). The
    # stamper flags sibling_strong on stage / forecast, not just the (capped) win/momentum number.
    if bool(rel.get("sibling_strong")):
        return True, f"existing account relationship — {nm[:40]} is a live deal on this account (win {round(bw)} / momentum {round(bm)}); we're already in — rapport and access carry", bm or None
    return False, "", (bm or None)


# --- §8.5 RFP / PROCESS-MODE (VP patch, 2026-07-07): during a structured RFP/tender the
# buyer runs the clock — quiet between deliverables is process cadence, not stalling.
# Guarded so a dead deal can't hide behind "we're in an RFP" (anti-zombie rules below).
_PROC_STAGES = ("formal evaluation", "shortlist", "vendor selected")
_PROC_KW = re.compile(r"rfp|rfi|rfq|bafo|tender|demo|orals|clarificat|infosec|info sec|security review"
                      r"|legal review|redlin|sow|proposal|submission|due|award|decis|decid|workshop"
                      r"|presentation|evaluation|down[- ]?select|pricing|cfo|exco|steerco|board (review|meeting)", re.I)
_PAUSE_KW = re.compile(r"postpon|on hold|hold until|budget freeze|re-?baselin|next quarter|paused"
                       r"|deferred|frozen|pushed to (q[1-4]|next)", re.I)
_MONTH_N = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}


def _milestone_dates(text):
    """All parseable dates in a next-step log — in the formats REPS ACTUALLY WRITE:
    ISO 2026-07-17 · '8 Jul 2026' · 'Jun 29, 2026' · '13th July' / '17-Jul' (year-less)
    · 'July 13' (year-less) · European '17.7.' / '17.7.2026'. Year-less dates assume the
    current year and are kept only within ±300 days (near-term milestones / recent log)."""
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).date()
    out = set()

    def _add(y, mo, d, yearless=False):
        try:
            dt = _dt.date(int(y), int(mo), int(d))
        except (ValueError, TypeError):
            return
        if yearless and abs((dt - today).days) > 300:
            return
        out.add(dt)

    t = str(text or "")
    _mon = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    for m in re.finditer(r"\b(20\d{2})-(\d{2})-(\d{2})\b", t):
        _add(m.group(1), m.group(2), m.group(3))
    for m in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _mon + r"[a-z]*\.?,?\s+(20\d{2})\b", t, re.I):
        _add(m.group(3), _MONTH_N[m.group(2).lower()], m.group(1))
    for m in re.finditer(_mon + r"[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(20\d{2})\b", t, re.I):
        _add(m.group(3), _MONTH_N[m.group(1).lower()], m.group(2))
    for m in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s.-]+" + _mon + r"[a-z]*\b(?!\.?,?\s*20\d{2})", t, re.I):
        _add(today.year, _MONTH_N[m.group(2).lower()], m.group(1), yearless=True)
    for m in re.finditer(_mon + r"[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!,?\s*20\d{2})(?![./\d])", t, re.I):
        _add(today.year, _MONTH_N[m.group(1).lower()], m.group(2), yearless=True)
    for m in re.finditer(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b", t):
        _add(m.group(3), m.group(2), m.group(1))
    for m in re.finditer(r"\b(\d{1,2})\.(\d{1,2})\.(?!\d)", t):
        _add(today.year, m.group(2), m.group(1), yearless=True)
    return sorted(out)


def _process_mode(hard, stage_l, la_days):
    """(on: bool, why: str) — §8.5. ON requires: structured stage + a live FUTURE dated process
    milestone + on-track (no pause language, no deadline passed in silence)."""
    import datetime as _dt
    if not any(s in stage_l for s in _PROC_STAGES):
        return False, ""
    text = str(hard.get("next_step") or "")
    if not text or not _PROC_KW.search(text):
        return False, ""
    if _PAUSE_KW.search(text):
        return False, "pause language in the next-step log"
    today = _dt.datetime.now(_dt.timezone.utc).date()
    dates = _milestone_dates(text)
    future = [d for d in dates if d >= today]
    past = [d for d in dates if d < today]
    if not future:
        return False, ""                      # nothing scheduled -> dead-quiet, not RFP-quiet
    # anti-zombie: a deliverable date PASSED with no movement since (no activity of ours or
    # the buyer's after it) -> the process itself stalled.
    if past:
        newest_past_age = (today - max(past)).days
        if la_days is None or la_days > newest_past_age + 2:
            return False, f"deliverable dated {max(past).isoformat()} passed with no movement"
    nearest = min(future)
    return True, (f"structured {stage_l or 'evaluation'} process on-track — next dated milestone "
                  f"{nearest.isoformat()}; quiet between deliverables is process cadence")


# --- DORMANT / ON-HOLD FLOOR (2026-07-09, Galp) -------------------------------------------------
# A deal the BUYER has explicitly PARKED — merger freeze, reprioritisation, "on hold", "shelved",
# "relaunch in <future year>", a fired "closed-lost-if-no-feedback" trigger — is NOT "moving right
# now"; momentum must read stalled. The leak this closes: a single status-update email confirming
# the freeze ("we're on hold until 2028") lands as last_buyer_touch <30d, so the stalling drag reads
# 0 and the deal shows "Steady" — Galp 46 "On Hold", MTR 79 "frozen", National Holding 84 "on hold".
# A status email SAYING the deal is parked is the OPPOSITE of engagement, not evidence of it.
# High precision: fires ONLY when the rep's CURRENT Next Step (authoritative "where is this deal
# now") or the live verdict says parked AND engagement is genuinely inert (~0 real buyer sessions),
# so a busy live deal that merely references a past hold is untouched (Temasek eng 24.6, John Deere
# eng 30 stay live — those are a separate engagement-quality question, not dormancy).
_DORMANT_PARK_RE = re.compile(
    r"(on[\s\-]?hold|\bsuspend|\bfrozen\b|\bfreeze\b|\bshelved\b|\bparked\b|\bdormant\b|"
    r"deprioriti[sz]|de[\s\-]?prioriti[sz]|no (?:more )?visibilit\w* (?:on|into) priorit|"
    r"back burner|closed lost due|(?:re[\s\-]?launch|relaunch|revisit)\w*\s+(?:in|by|before|next|end)\b)",
    re.I)
_DORMANT_FUTUREYEAR_RE = re.compile(r"\b(202[89]|203\d)\b")
DORMANT_MOM_CAP = 8.0   # a parked deal reads firmly "Slowing/stalled" (<35), whatever the recent email cadence


def _dormant_read(record: dict):
    """(is_dormant, why): the buyer has explicitly PARKED this deal AND it is genuinely inert.
    Keyed on the rep's CURRENT Next Step / the live verdict summary, corroborated by near-zero
    engagement so a live deal that merely references a past hold never trips it."""
    ai = (record or {}).get("ai") or {}
    hard = (record or {}).get("hard") or {}
    ns = str(hard.get("next_step") or "")
    vs = str((ai.get("north_star_verdict") or {}).get("summary") or "")
    m = _DORMANT_PARK_RE.search(ns) or _DORMANT_PARK_RE.search(vs)
    if not m:
        return False, ""
    eng = (ai.get("footprints") or {}).get("engagement") or {}
    try:
        pts = float(eng.get("points_60d"))
    except (TypeError, ValueError):
        pts = 0.0
    conf = _parse_confidence(ns)
    low_conf = isinstance(conf, (int, float)) and conf < 35
    future = bool(_DORMANT_FUTUREYEAR_RE.search(ns))
    # Near-zero engagement is sufficient on its own (no real buyer sessions = inert). A weaker
    # engagement reading needs a second inertness flag — rep confidence <35% or a future-year relaunch.
    if not ((pts < 2.0) or (pts < 6.0 and (low_conf or future))):
        return False, ""
    _fy = _DORMANT_FUTUREYEAR_RE.search(ns)
    return True, (f"buyer has parked the deal (next step: '{m.group(0)}'"
                  + (f", relaunch not before {_fy.group(0)}" if _fy else "")
                  + f") with no live engagement (engagement pts {round(pts, 1)}) — a status update "
                  "confirming a freeze is not buyer momentum; dormant")


def score_momentum_v2(record: dict):
    """Momentum v5 (2026-07-07, VP spec adopted with amendments): momentum = the PULSE of buyer
    engagement + forward progression, recency-weighted. Engagement points (type × who × steep
    decay, cap +35) are the dominant term; stalling is judged against the STAGE's expected
    cadence (graduated 0…−25); a close push within 60d tolerance costs nothing (the slip signal
    lives in Win Position); forecast/stage up-moves credit +6. CARRIED OVER unchanged: AI-text
    exclusion, buyer replies count, the false-velocity guard (only fires with no progression AND
    not engaged), late-stage email/contract-cadence softening."""
    ai = (record or {}).get("ai") or {}
    hard = (record or {}).get("hard") or {}
    fp = ai.get("footprints") or {}
    eng = fp.get("engagement") or {}
    ot = ai.get("opp_trends") or {}
    contribs = []
    score = 50.0

    # ---- engagement state first (drives everything else) --------------------------------
    # GENUINE buyer touch = a meeting OR an INBOUND buyer reply (footprints.last_buyer_touch —
    # Outreach '[Email] [In]' / 'Email Received' tasks classify as buyer touches). The Alghanim
    # bug: the buyer replied 3x on Jun-30, footprints knew it (buyer_touches_30d=3), but the
    # scorer only read last_meeting (Apr-23) and called the buyer 'dark 74d'.
    lm_days = _days_since(fp.get("last_meeting"))
    _bt_raw = _days_since(fp.get("last_buyer_touch"))
    bt_days = min([d for d in (lm_days, _bt_raw) if d is not None], default=None)
    _la_days = min([d for d in (_days_since(hard.get("last_activity_date")),
                                _days_since(fp.get("general_last_activity"))) if d is not None],
                   default=None)
    try:
        ev30 = int(eng.get("events_30d") or 0)
    except (TypeError, ValueError):
        ev30 = 0
    try:
        bt30 = int(fp.get("buyer_touches_30d") or 0)
    except (TypeError, ValueError):
        bt30 = 0
    _stage_l = str(hard.get("stage") or "").strip().lower()
    _late = any(t in _stage_l for t in ("shortlist", "selected", "contract", "negotiat", "award"))
    engaged = ((bt_days is not None and bt_days <= 30) or ev30 >= 2 or bt30 >= 1
               or (_late and _la_days is not None and _la_days <= 10))
    _via = ("meeting" if (lm_days is not None and (bt_days == lm_days)) else "buyer reply")
    _cad_map = {"qualified": 30.0, "formal evaluation": 21.0, "shortlisted": 18.0,
                "vendor selected": 14.0, "contracting": 14.0}
    try:
        cadence = float(fp.get("stage_cadence_days") or 0) or _cad_map.get(_stage_l, 21.0)
    except (TypeError, ValueError):
        cadence = _cad_map.get(_stage_l, 21.0)
    # §8.5 RFP / process-mode — buyer runs the clock; deliverable-driven quiet ≠ stalling.
    process_mode, pm_why = _process_mode(hard, _stage_l, _la_days)

    # PRIMARY 1 — ENGAGEMENT POINTS (VP spec §2A/§3/§4: Σ type-weight × who × recency decay,
    # cap +35 — the dominant term). Two clocks (§8.5 v1.1): the fast-cycle points_60d normally;
    # the STRETCHED process clock (points_90d_process: ≤30d ×1.0 / ≤60d ×0.5 / ≤90d ×0.2) while
    # a structured RFP/tender is on-track — deliverables run 3-6 weeks apart, so a month-old
    # demo cluster is recent and keeps real weight. Legacy records fall back to volume buckets.
    _pts_raw = eng.get("points_90d_process") if process_mode and isinstance(eng.get("points_90d_process"), (int, float)) \
        else eng.get("points_60d")
    if isinstance(_pts_raw, (int, float)):
        epts = round(min(35.0, float(_pts_raw)), 1)
        esrc = (f"engagement points {epts} on the RFP process clock (month-old deliverable work keeps weight)"
                if process_mode else f"engagement points {epts} (type × buyer/rep × recency over 60d)")
    else:
        _vol = max(ev30, bt30)
        epts = 18.0 if _vol >= 8 else 12.0 if _vol >= 4 else 7.0 if _vol >= 2 else 3.0 if _vol >= 1 else 0.0
        top = float(eng.get("raw_top") or eng.get("top_weight") or 0.0)
        if top >= 6 and _is_real_session(eng.get("top_event")) and lm_days is not None and lm_days <= 30:
            epts = min(35.0, epts + round(min(8.0, (top - 5.0) * 2.5), 1))
        _vsrc = "session(s)" if ev30 >= bt30 else "inbound buyer repl(ies)"
        esrc = f"{_vol} {_vsrc} in the last 30d (legacy volume read)"
        if process_mode:
            # legacy record on the process clock: a 60d meeting cluster is on-cycle work
            try:
                m60 = int(fp.get("meetings_60d") or 0)
            except (TypeError, ValueError):
                m60 = 0
            _proc_e = 16.0 if m60 >= 8 else 12.0 if m60 >= 4 else 8.0 if m60 >= 2 else 5.0 if m60 >= 1 else 0.0
            if _proc_e > epts:
                epts = _proc_e
                esrc = f"{m60} meeting(s) in the last 60d — on-cycle RFP work (process clock, legacy read)"
    # freshness floor (§3): any genuine buyer touch in the last 14 days can't read below steady
    if bt_days is not None and bt_days <= 14 and epts < 8.0:
        epts = 8.0
        esrc = f"buyer touched the deal {bt_days}d ago ({_via}) — freshness floor"
    # DECLINE DISCOUNT (2026-07-08, user-directed): engagement rewards meeting VOLUME regardless of
    # DIRECTION — a busy deal being renegotiated DOWNWARD banks near-max points. When the deal is
    # trending down (forecast downgrade / scope cut / a real close slip), those meetings are worth
    # LESS, scaled by how many axes are declining. Deliberately STACKS with the flat decline
    # penalties below (regression / scope_cut): lots of talk about shrinking a deal is not momentum.
    # (Austrian Post: onsite + pricing rounds banked engagement ~34 while amount −31%, forecast cut,
    # close slipped — momentum still read hot until the busywork itself was discounted.)
    # Keyed on the two UNAMBIGUOUS "deal getting worse in substance" signals — a forecast DOWNGRADE
    # and a SCOPE / amount CUT. A close-date slip is timing (already handled with tolerance by the
    # close term) and is deliberately NOT counted here, so the discount only fires on deals whose
    # substance is declining, never on a healthy deal that merely rescheduled.
    if epts > 0:
        _decl = 0
        _fct, _amt = ot.get("forecast_category_trend"), ot.get("amount_trend")
        if isinstance(_fct, (int, float)) and _fct < -0.02:
            _decl += 1                      # forecast downgraded
        if isinstance(_amt, (int, float)) and _amt < -0.2:
            _decl += 1                      # scope / amount cut
        if _decl:
            _fac = 0.82 if _decl == 1 else 0.66     # one axis: mild; both: firm
            epts = round(epts * _fac, 1)
            esrc += f" — discounted ×{_fac} (deal trending DOWN on {_decl} " + ("axis" if _decl == 1 else "axes") + ")"
    if epts:
        score += epts
        contribs.append(_contrib("engagement", round(epts, 1), esrc))

    # PRIMARY 2 — STAGE-RELATIVE STALLING DRAG (VP spec §8: quiet vs what the STAGE needs,
    # graduated 0…−25; replaces the old flat −8/−12). Late-stage email/contract cadence
    # (award, NDA, redlines) still softens the drag — that's real buyer motion.
    # §8.5: SUSPENDED entirely while a structured RFP/tender process is on-track.
    if process_mode:
        drag = 0.0
        dwhy = pm_why
    elif bt_days is None:
        # DATA ABSENCE ≠ BUYER DARK (2026-07-07): a record with NO activity instrumentation at
        # all (footprints never built — pre-footprints sweep) must not take the full dark slam;
        # we don't KNOW the buyer is silent, we know we haven't measured. Scale a mild drag by
        # the only signal available (Salesforce last-activity). A record that IS instrumented
        # and still shows zero buyer touches earns the full dark read — that's proven silence.
        _no_instr = not (fp.get("last_buyer_touch") or fp.get("last_meeting")
                         or fp.get("buyer_touches_30d") or fp.get("meetings_60d"))
        if _no_instr:
            if _la_days is not None and _la_days <= 30:
                drag = 4.0
            elif _la_days is not None and _la_days <= 60:
                drag = 8.0
            else:
                drag = 12.0
            dwhy = (f"no activity instrumentation on this record yet (cadence reads fully after "
                    f"its next sweep){f' — Salesforce shows activity {_la_days}d ago' if _la_days is not None else ''}")
        else:
            drag = 25.0
            dwhy = "no genuine buyer touch on record — buyer side dark"
    elif bt_days <= cadence:
        drag = 0.0
        dwhy = f"buyer touch {bt_days}d ago — within the ~{int(cadence)}d cadence this stage needs"
    else:
        drag = min(25.0, (bt_days / cadence - 1.0) * 12.0)
        dwhy = (f"last genuine buyer touch {bt_days}d ago vs the ~{int(cadence)}d cadence this "
                f"stage needs — going quiet")
    if drag > 4.0 and _late and _la_days is not None and _la_days <= 10:
        drag = 4.0
        dwhy = (f"no recent meeting, but active deal flow {_la_days}d ago — late-stage "
                f"email/contract cadence (award/redlines/scheduling), not buyer silence")
    if drag:
        score -= drag
    contribs.append(_contrib("stalling" if drag else "cadence", round(-drag, 1), dwhy))

    # PRIMARY 3 — CLOSE-DATE MOVE with TOLERANCE (VP spec §6): a normal, explainable push
    # (≤60d) costs NOTHING; only beyond-tolerance slips drag (capped −10, scaled); a pull-in
    # earns up to +5. The slip signal itself lives in Win Position (trend nudge), not here.
    cdt = ot.get("close_date_trend")
    if isinstance(cdt, (int, float)) and abs(cdt) > 0.02:
        det = ot.get("close_date_trend_detail") or ""
        _m = re.search(r"(pushed|pulled)\s*(?:out|in|forward)?\s*(?:by\s*)?(\d+)\s*d", str(det), re.I)
        days = int(_m.group(2)) if _m else None
        if cdt > 0:      # pulled in — accelerating
            cpts = min(5.0, (days / 10.0) if days else 3.0)
            det = (det or "close date pulled in") + " — accelerating"
        elif days is not None and days <= 60:
            cpts = 0.0
            det = (det or "close date pushed") + " — within tolerance (≤60d), a timing move priced into Win, not lost momentum"
        elif days is not None:
            # BEYOND-TOLERANCE (2026-07-08): the 60d grace is a THRESHOLD, not a deductible — once
            # crossed, the WHOLE push is charged (base −5, ramping to −12), never a from-zero ramp.
            # Old formula: −(days−60)/12 → a 61d push cost −0.1 (a two-month slip ≈ free; SAMI read
            # momentum 95 with close pushed 61d). Now 61d ≈ −5.1, 130d+ = −12.
            cpts = -min(12.0, 5.0 + (days - 60) / 10.0)
            det = (det or "close date pushed") + f" — {days}d cumulative push is beyond tolerance"
        else:
            cpts = -4.0 if cdt < -0.3 else 0.0
            det = (det or "close date pushed out") + " — push magnitude unknown, modest drag"
        _cp_lvl = str(((ai.get("customer_preference") or {}).get("level")
                       or (ai.get("customer_preference") or {}).get("status") or "")).lower()
        if cpts < -6.0 and _cp_lvl == "high":
            cpts = -6.0
            det = str(det) + " — selection already made; push reflects contracting timeline, drag capped"
        cpts = round(cpts, 1)
        if cpts:
            score += cpts
        contribs.append(_contrib("close_date_direction", cpts, det))

    # PRIMARY 4 — STAGE / FORECAST DIRECTION (VP spec §7, SYMMETRIC 2026-07-08): a recent up-move is
    # real advance (+6); a DOWNGRADE (forecast cut, e.g. Best Case -> Upside) or stage regression is
    # real DECLINE (-10). A deal moving BACKWARDS is NOT high-momentum however busy it looks — the
    # rep lowering their OWN forecast is a strong negative signal, and momentum was previously blind
    # to it (only ever added +6, never subtracted), so a shrinking/downgraded/slipping deal still
    # read hot on raw meeting volume (Austrian Post: forecast cut + amount -31% + close +23d → 90).
    _up = _down = None
    for k in ("stage_trend", "forecast_trend", "forecast_category_trend"):
        v = ot.get(k)
        if isinstance(v, (int, float)):
            if v > 0.02 and _up is None:
                _up = k
            elif v < -0.02 and _down is None:
                _down = k
        elif isinstance(v, str):
            lv = v.lower()
            if any(t in lv for t in ("up", "advanc", "upgrad", "raised")) and _up is None:
                _up = k
            elif any(t in lv for t in ("down", "regress", "downgrad", "lower", "cut")) and _down is None:
                _down = k
    if _down:                          # a downgrade/regression DOMINATES a stale up-move
        score -= 10.0
        contribs.append(_contrib("regression", -10.0,
                                 f"{_down.replace('_', ' ')}: moved DOWN recently — forecast/stage cut, deal regressing"))
    elif _up:
        score += 6.0
        contribs.append(_contrib("progression", 6.0, f"{_up.replace('_', ' ')}: moved UP recently — deal advancing"))
    # SCOPE / AMOUNT CUT — a deal renegotiated SMALLER is contracting, not advancing; drag momentum.
    _amt = ot.get("amount_trend")
    if isinstance(_amt, (int, float)) and _amt < -0.2:
        score -= 6.0
        contribs.append(_contrib("scope_cut", -6.0,
                                 f"deal size cut recently ({ot.get('amount_trend_detail') or 'amount reduced'}) — contracting, not advancing"))

    # NEXT-STEP PLAN TERMS (VP §2B/§2C, user-ratified via Techtronic): a live, dated,
    # ADVANCING plan is forward motion — +8 when ≥2 dated milestones with one in the future,
    # +3 more for a rich trail (≥4). THEATRE GUARD: halved when the buyer is quiet >30d
    # outside a structured process — a rep typing dates into silence is not momentum
    # (false-velocity still caps the genuinely slipping ones).
    import datetime as _dtns
    _today_ns = _dtns.datetime.now(_dtns.timezone.utc).date()
    _all_ms = _milestone_dates(hard.get("next_step"))
    # dated count: legacy counter ORed with the full rep-date parser ('1st July', '17.7.' etc.).
    # Retained ONLY for the false-velocity signal below (a BUSY log — past edits included — on a
    # slipping deal). It must NOT drive the plan credit (see below).
    dated = max(_count_dated_milestones(hard.get("next_step")), len(_all_ms))
    # PLAN CREDIT = FORWARD, NEAR-TERM milestones only. The Next_Step field is usually a running
    # HISTORY JOURNAL, so counting all parsed dates rewards the past: Austrian Post's 10k-char log
    # parsed to 44 dates — 42 already past, the only 2 future ones in December, AFTER a 23-Jul close
    # — earned the full +11 "advancing plan" with ZERO real near-term milestone, pegging momentum to
    # 99. A live plan is milestones ahead of today and inside a ~90-day planning horizon (a lone
    # post-close placeholder is not a plan to hit THIS close). Count those; ignore the history.
    _horizon_ns = _today_ns + _dtns.timedelta(days=90)
    _plan_ms = [d for d in _all_ms if _today_ns <= d <= _horizon_ns]
    nspts = 0.0
    if len(_plan_ms) >= 1:
        nspts += 8.0
    if len(_plan_ms) >= 3:
        nspts += 3.0
    if nspts and not process_mode and (bt_days is None or bt_days > 30):
        nspts = round(nspts * 0.5, 1)
    # SUBSTANCE GATE (2026-07-08, Galp): a plan is only motion when real engagement backs it.
    # Dates typed into a near-dead deal are THEATRE: engagement ~0 halves the credit; rep's OWN
    # confidence <35% with no real engagement earns NOTHING (Galp: engagement 1.0, champion
    # cooling, confidence 10% — momentum read 57 off a +11 paper plan).
    if nspts and not process_mode and epts < 8.0:
        _cg = _parse_confidence(hard.get("next_step"))
        nspts = 0.0 if (isinstance(_cg, (int, float)) and _cg < 35) else round(nspts * 0.5, 1)
    if nspts:
        score += nspts
        contribs.append(_contrib("next_step_plan", round(nspts, 1),
                                 f"{len(_plan_ms)} upcoming milestone(s), nearest {min(_plan_ms).isoformat()} — live advancing plan"))

    # PRIMARY 5 — CONFIDENCE-% TRAJECTORY (the rep's own Probability/Confidence in the log).
    conf = _parse_confidence(hard.get("next_step"))
    if conf is None:
        conf = _num(hard.get("probability"))
    if isinstance(conf, (int, float)):
        cpts2 = -5.0 if conf < 35 else -3.0 if conf < 50 else 4.0 if conf >= 70 else 0.0
        if cpts2:
            score += cpts2
            contribs.append(_contrib("confidence", round(cpts2, 1),
                                     f"rep confidence {int(conf)}%" + (" (low)" if conf < 50 else " (high)")))

    # FALSE-VELOCITY (the Alghanim signature): a busy next-step log on a slipping deal is still
    # slipping. ≥3 dated lines but close pushed / confidence falling / buyer quiet >30d → hold low.
    slipping = ((isinstance(cdt, (int, float)) and cdt < -0.15)
                or (isinstance(conf, (int, float)) and conf < 40)
                or (bt_days is not None and bt_days > 30)
                or bt_days is None)
    # FALSE VELOCITY means "activity WITHOUT progression" — it can NEVER fire on a deal that
    # just progressed (stage/forecast up-move) or is actively engaged. The Bosch bug: stage
    # moved UP + active deal flow 8d ago, yet the cap slammed momentum 60 -> 25, which then
    # dragged Win by -31 on a $1.2M Shortlisted deal. §8.5: also suspended in an ON-TRACK
    # structured process — FUTURE-dated deliverables are the process plan, not fake motion
    # (the anti-zombie overdue rule already ejects deals whose deadlines passed in silence).
    # (`dated` computed above in the next-step plan block.)
    if dated >= 3 and slipping and not _up and not engaged and not process_mode:
        contribs.append(_contrib("false_velocity", 0.0,
                                 "activity without progression — busy next-step log but the deal is slipping "
                                 "(close pushed / confidence down / buyer quiet); edits don't raise momentum"))
        score = min(score, 25.0)

    # RELATIONSHIP MOMENTUM WRAP (2026-07-07 user spec): a strong sibling deal's momentum
    # partially carries onto an expansion/phase-2 opp on the same account — the rapport is
    # shared even when this record is young. 35% of the gap, cap +12, never lowers.
    rel_ok, rel_why, _rel_bm = _relationship_context(record)
    if rel_ok and isinstance(_rel_bm, (int, float)) and _rel_bm > score:
        wrap = round(min(REL_MOM_CAP, REL_MOM_FRACTION * (float(_rel_bm) - score)), 1)
        if wrap > 0:
            score += wrap
            contribs.append(_contrib("relationship_momentum", wrap,
                                     f"sister deal on this account is moving (momentum {round(float(_rel_bm))}) — account momentum partially carries over"))

    # §8.5 on-track floor: a deal waiting on the buyer's OWN published process is steady, not
    # slowing — momentum holds at 50 minimum while process-mode is on.
    if process_mode and score < 50.0:
        contribs.append(_contrib("process_floor", round(50.0 - score, 1),
                                 "on-track structured process — waiting on the buyer's published timeline reads steady, not slowing"))
        score = 50.0

    # VERDICT RECONCILE (2026-07-08, user-directed): ONE STORY PER DEAL. The north-star verdict is
    # the engine's own holistic judgment (it reads stalled milestones, stage-stuck time, blockers —
    # things the momentum terms don't); momentum must not contradict it. A NEGATIVE-verdict deal
    # cannot read "accelerating" off raw activity volume (SAMI: verdict Slowing — milestone passed
    # with no movement, 127d stuck in Shortlisted — while momentum read 95 "one of the hottest").
    # Slowing caps momentum at 60 ("active but slipping"); Off Track caps at 35. Applied AFTER the
    # process floor — a genuinely negative verdict out-votes "we're in a process".
    _nv = ai.get("north_star_verdict") or {}
    _verd = str(_nv.get("verdict") or "").strip().lower().replace("-", " ")
    _vcap = 60.0 if _verd == "slowing" else (35.0 if _verd == "off track" else None)
    if _vcap is not None and score > _vcap:
        contribs.append(_contrib("verdict_reconcile", round(_vcap - score, 1),
                                 f"the deal's own verdict is '{_nv.get('verdict')}' — momentum cannot read "
                                 f"hotter than the holistic judgment; capped at {int(_vcap)}"))
        score = _vcap

    # DORMANT / ON-HOLD FLOOR (2026-07-09, Galp): a buyer-parked, inert deal cannot read "Steady"
    # off a status-update email that itself confirms the freeze. Strongest cap — applied last.
    _dorm, _dorm_why = _dormant_read(record)
    if _dorm and score > DORMANT_MOM_CAP:
        contribs.append(_contrib("dormant_floor", round(DORMANT_MOM_CAP - score, 1), _dorm_why))
        score = DORMANT_MOM_CAP

    score = round(_clamp(score, 0.0, 99.0), 1)
    return {"score": score, "pre_decay": score, "decay_note": None,
            "model": "engagement_v5", "contributions": contribs}


# Scope-shrink (user-directed, Techtronic): a deal NARROWING vs its prior/original scope
# (Source-to-Pay -> Source-to-Contract, modules dropped, amount cut with fewer products) is
# the buyer getting DEFENSIVE — usually cost-cutting, or an implementation/integration
# concern (wanting phased over big-bang). It costs Win a fixed ~7 points (bounded, one-time)
# and is surfaced for the CEO monitor. Driven by the sweep's ai.scope_change signal.
_SCOPE_SHRINK_PTS = -7.0
_SCOPE_REDUCED = ("reduced", "reduced_scope", "shrunk", "shrinking", "narrowed", "narrowing", "down")


def _scope_shrink(record):
    """(shrunk?, why) from ai.scope_change; ('', ) when absent/stable/expanded."""
    sc = ((record or {}).get("ai") or {}).get("scope_change")
    if not isinstance(sc, dict):
        return False, ""
    if str(sc.get("direction") or "").strip().lower() in _SCOPE_REDUCED:
        return True, str(sc.get("detail") or sc.get("to") or "scope narrowed vs prior")
    return False, ""


# --- Selection override (2026-07-07 spec §4): a CONFIRMED SELECTION whose CRM StageName still
# lags is anchored to the Vendor-Selected floor (72) with the full 100 ceiling unlocked. TRIPLE-
# gated so an ordinary shortlisted deal is never touched, and it can ONLY RAISE Win, never lower.
SELECTION_FLOOR = 72.0
_SELECTION_PREF_MIN = 0.9


def _selection_override(record: dict, strengths: Optional[dict] = None) -> bool:
    ai = (record or {}).get("ai") or {}
    hard = (record or {}).get("hard") or {}
    # 0) STAGE GATE (2026-07-07): a SELECTION cannot precede an EVALUATION. The override unlocks
    # the full 100 ceiling for a confirmed selection whose CRM stage LAGS — but that only makes
    # sense at/after a formal evaluation. An early-stage deal (Initial Interest / Qualified /
    # discovery) claiming selection is a mis-read, not a lagging CRM; its pre-RFP ceiling (<=30)
    # MUST hold (PremiStar: a 7-day Qualified deal, one discovery call, 'no appetite for new
    # platforms', read 99). Unknown stage is also blocked — can't confirm we're post-evaluation.
    _stg = str(hard.get("stage") or "").strip().lower()
    if not _stg or any(t in _stg for t in ("initial interest", "qualified", "prospect", "discovery", "lead", "1.", "2.")):
        return False
    # 1) ACCESS TO POWER (2026-07-07, user-directed 7-point drill): a selection is MADE BY an
    # economic buyer. With NO confirmed EB on record there is no one who could have selected us —
    # a "confirmed selection" claim on inferred preference is a mis-read (Barnes & Noble:
    # economic_buyer gap, one stale "checks the boxes" quote, still read 99). Hard fact, not a lean.
    if str(((ai.get("meddpicc") or {}).get("economic_buyer") or {}).get("status") or "").strip().lower() != "confirmed":
        return False
    # 2) The engine's OWN verdict must not be weak — a Slowing / at-risk deal is not a selection.
    nv = ai.get("north_star_verdict") or {}
    if str(nv.get("verdict") or "").strip().lower() in ("slowing", "stalled", "at risk", "at_risk", "cooling", "declining"):
        return False
    st = strengths if isinstance(strengths, dict) else _rubric_win_strengths(record or {})
    # 3) HIGH stated preference ("you've chosen Zycus", "best platform").
    cp = ai.get("customer_preference") or {}
    _ps = st.get("preference")
    pref_high = (str(cp.get("level") or cp.get("status") or "").lower() == "high"
                 or (isinstance(_ps, (int, float)) and float(_ps) >= _SELECTION_PREF_MIN))
    if not pref_high:
        return False
    # 4) A POSITIVE competitive edge — 'unknown rivals' (a data gap, competitive_strength ~0) does
    # NOT count as 'no rival ahead'. Require actual evidence we're ahead of the named field.
    if _competitive_strength(ai) <= 0:
        return False
    # 5) A REAL selection/commit signal — not merely 'forecast_defensible', which defends even a
    # Pipeline call (B&N's verdict was forecast_defensible=true while recommending Pipeline). Needs
    # a recorded won/selected decision, or the north-star recommending COMMIT. Best Case is UPSIDE,
    # not a confirmed selection — it must NOT unlock the ceiling (Global Switch, Best Case, → 70).
    dec = str((ai.get("decision_outcome") or {}).get("status") or "").strip().lower()
    rec_fc = str(nv.get("recommended_forecast") or "").strip().lower()
    return dec in ("won", "selected") or rec_fc.startswith("commit")


# --- Qualification gate (2026-07-07, user-directed "7-point drill") -----------------------------
# A HIGH Win probability must be EARNED by ticking the qualification boxes first. ACCESS TO POWER
# (economic buyer) is the dominant gate — you cannot be confident of winning a deal you have no path
# to get signed. Win is CEILINGED by qualification AFTER the raw compute, so momentum / stated
# preference cannot lift a deal past what its boxes support (Barnes & Noble: economic_buyer gap,
# single-threaded to a Manager, unknown competitive field — yet read 99). Once the HARD SF stage is
# Vendor Selected+ the stage ITSELF proves access to power, so the cap lifts (Publicis / Swift / Mair
# untouched). Competitive visibility and champion depth also gate the top.
QUAL_EB_CEILING = {"confirmed": 100.0, "partial": 74.0, "gap": 52.0}
QUAL_COMP_CEILING = {"confirmed": 100.0, "partial": 90.0, "gap": 66.0}
QUAL_CHAMP_CEILING = {"confirmed": 100.0, "partial": 86.0, "gap": 60.0}
_QUAL_MISSING = {"economic_buyer": 50.0, "competition": 66.0, "champion": 58.0}
_QUAL_POST_SELECTION = ("vendor select", "selected", "negotiat", "contract", "won",
                        "po received", "po-received", "closed")


def _eb_status_floored(record: dict) -> str:
    """Effective economic_buyer status, after two floors:

    1. eb_engagement (call-evidence layer, highest priority): if the sweep emitted
       ai.eb_engagement the call-sourced strength drives the status directly —
       direct face-time → confirmed, indirect involvement → partial,
       field_only/none → gap, overriding whatever MEDDPICC says.
       This closes the symmetric gap to champion_strength: a CRM checkbox for EB
       can no longer masquerade as real exec engagement.

    2. Hard-flag floor (original guard): if Salesforce's own eb_identified=False AND
       sweep evidence is thin AND MEDDPICC says partial/confirmed → floor to 'gap'.
       (Avaya fix — eb_identified False + empty evidence + MEDDPICC 'partial' → 70.)

    A genuinely evidenced EB (direct face time confirmed) is always untouched."""
    ai = (record or {}).get("ai") or {}
    hard = (record or {}).get("hard") or {}

    # --- Layer 1: call-evidence override (eb_engagement, if present) ---
    eb_eng = ai.get("eb_engagement") or {}
    eb_str = str(eb_eng.get("strength") or "").strip().lower()
    if eb_str == "direct":
        return "confirmed"
    if eb_str == "indirect":
        return "partial"
    if eb_str in ("field_only", "none"):
        return "gap"

    # --- Layer 2: existing hard-flag floor (no eb_engagement present) ---
    md = ai.get("meddpicc") or {}
    ebd = md.get("economic_buyer") if isinstance(md.get("economic_buyer"), dict) else {}
    stat = str((ebd.get("status") if isinstance(ebd, dict) else ebd) or "").strip().lower()
    evi = str((ebd.get("evidence") if isinstance(ebd, dict) else "") or "").strip()
    if hard.get("eb_identified") is False and len(evi) < 10 and stat in ("partial", "confirmed"):
        return "gap"
    return stat


def _qualification_ceiling(record: dict):
    """(cap, box_label, status): the highest Win the deal's qualification supports. Access to Power
    dominates; competitive visibility and champion depth also gate the top. Post-selection stages
    return 100 — the hard SF stage already proves selection."""
    hard = (record or {}).get("hard") or {}
    ai = (record or {}).get("ai") or {}
    stage = str(hard.get("stage") or "").strip().lower()
    if any(t in stage for t in _QUAL_POST_SELECTION):
        return 100.0, "", ""
    md = ai.get("meddpicc") or {}

    def _st(k):
        v = md.get(k) if isinstance(md, dict) else None
        return str((v.get("status") if isinstance(v, dict) else v) or "").strip().lower()

    eb, comp, champ = _eb_status_floored(record), _st("competition"), _st("champion")
    # Expansion into a WON account: we already hold executive / seat access — relax the EB gate.
    exp = ai.get("expansion_context")
    if isinstance(exp, dict) and exp.get("prior_closed_won") and eb in ("gap", ""):
        eb = "partial"
    caps = [
        (QUAL_EB_CEILING.get(eb, _QUAL_MISSING["economic_buyer"]), "access to power (economic buyer)", eb or "missing"),
        (QUAL_COMP_CEILING.get(comp, _QUAL_MISSING["competition"]), "competitive visibility", comp or "missing"),
        (QUAL_CHAMP_CEILING.get(champ, _QUAL_MISSING["champion"]), "champion", champ or "missing"),
    ]
    return min(caps, key=lambda x: x[0])


# --- High-risk penalty (2026-07-07 spec §5): Risk is folded INTO Win — genuinely high risk
# lowers winnability. Noise floor 20 (early/thin-read risk ignored); 0.5×(Risk-20), cap -30.
WIN_RISK_NOISE_FLOOR = 20.0
WIN_RISK_PENALTY_RATE = 0.5
WIN_RISK_PENALTY_CAP = 30.0


def _win_risk_penalty(deal_risk) -> float:
    if not isinstance(deal_risk, (int, float)):
        return 0.0
    over = float(deal_risk) - WIN_RISK_NOISE_FLOOR
    return min(WIN_RISK_PENALTY_CAP, WIN_RISK_PENALTY_RATE * over) if over > 0 else 0.0


# --- Forecast-conviction credit (2026-07-07 spec §5): a manager's elevated ForecastCategory is
# first-class evidence — it ADDS to Win, gated on the call being evidence-consistent (verdict
# forecast-defensible OR opp trends net-positive). A sandbagged/inflated upgrade gets no credit.
WIN_FORECAST_CREDIT = {"commit": 7.0, "best case": 4.0, "upside key deal": 4.0, "upside": 4.0}


def _forecast_conviction_credit(record):
    hard = (record or {}).get("hard") or {}
    fc = str(hard.get("forecast_category") or "").strip().lower()
    credit = WIN_FORECAST_CREDIT.get(fc, 0.0)
    if credit <= 0:
        return 0.0, ""
    ai = (record or {}).get("ai") or {}
    defensible = bool((ai.get("north_star_verdict") or {}).get("forecast_defensible"))
    tnet, _ = _opp_trend_net(record or {})
    if defensible or (isinstance(tnet, (int, float)) and tnet > 0):
        return credit, f"{hard.get('forecast_category')} forecast is evidence-consistent — manager conviction credited"
    return 0.0, ""   # sandbagged / inflated with no supporting evidence -> no credit


def score_win_position(ev, record=None, momentum=None, deal_risk=None):
    anchor = _win_anchor(record)
    strengths = _rubric_win_strengths(record or {})
    # CRM/Next-Step source per factor, so each contribution can SAY WHY (the reason feature).
    _crm = ((record or {}).get("ai") or {}).get("crm_evidence") or {}
    contributions, weighted = [], 0.0
    for f, w in RUBRIC_WIN_WEIGHTS.items():
        s = strengths.get(f, WIN_MISSING)
        weighted += w * s
        why = f"{f.replace('_', ' ')} strength {s:+.2f} (weight {w})"
        src = _crm.get(f)
        if isinstance(src, dict) and src.get("present") and s > 0:
            why += f" — from {src.get('src') or 'CRM'}: {src.get('value') or 'present'}"
        contributions.append(_contrib(f, round(WIN_RUBRIC_BAND * w * s / 100.0, 1), why))
    net = max(-1.0, min(1.0, weighted / 100.0))   # rubric net in [-1,+1]

    # Opportunity-trend nudge (CRM moves are buying/loss signals): blend into the net so
    # progression (amount up, close pulled in, stage/category advanced) lifts Win and
    # regression chips it off — still within the +/-30 band.
    tnet, tdetail = _opp_trend_net(record or {})
    if tnet is not None:
        net = max(-1.0, min(1.0, net + WIN_TREND_INFLUENCE * tnet))
        for k, v in tdetail:
            contributions.append(_contrib(k, round(WIN_RUBRIC_BAND * WIN_TREND_INFLUENCE
                                                    * WIN_TREND_WEIGHTS[k] * v / sum(WIN_TREND_WEIGHTS.values()), 1),
                                          f"{k.replace('_', ' ')} {v:+.2f}"))

    adj = round(WIN_RUBRIC_BAND * net, 1)         # signed: strong adds, weak/missing chips off

    # Momentum -> Win (BIDIRECTIONAL): the stage anchor isn't earned if the stage's expected
    # motion isn't happening. BELOW-expected momentum chips Win off fast (x1.0, no floor);
    # ABOVE-expected adds muscle (x0.5). Ceiling still caps the top.
    mom_adj = 0.0
    if isinstance(momentum, (int, float)):
        exp = _expected_momentum(record)
        delta = float(momentum) - exp
        rate = WIN_MOMENTUM_UP_RATE if delta >= 0 else WIN_MOMENTUM_DOWN_RATE
        mom_adj = round(delta * rate, 1)
        if abs(mom_adj) >= 0.1:
            contributions.append(_contrib("momentum_adj", mom_adj,
                                          f"momentum {round(float(momentum))} vs stage-expected {int(exp)}"))

    # Scope-shrink drag: a deal narrowing vs its prior scope loses a fixed ~7 points.
    shrunk, shrink_why = _scope_shrink(record)
    scope_pts = 0.0
    if shrunk:
        scope_pts = _SCOPE_SHRINK_PTS
        contributions.append(_contrib("scope_reduced", scope_pts,
                                      f"scope narrowed vs prior — {shrink_why[:160]} (buyer likely "
                                      f"defensive on cost or phased implementation)"))

    ceiling = _win_ceiling(record)                # stage cap: pre-RFP 30 / RFP 70 / post 100

    # QUALIFICATION GATE (7-point drill): a high Win must be EARNED — Access to Power (economic
    # buyer) dominates. Caps the stage ceiling so momentum / stated preference can't lift a deal
    # past what its qualification supports (B&N: economic_buyer gap -> capped 52, not 99).
    qual_cap, qual_box, qual_status = _qualification_ceiling(record)
    if qual_cap < ceiling:
        contributions.append(_contrib("qualification_gate", 0.0,
                                      f"Win capped at {int(qual_cap)}: {qual_box} not established "
                                      f"({qual_status}) — a higher win probability has to be earned "
                                      f"by qualifying this first"))
        ceiling = qual_cap

    # §4 Selection override — a confirmed selection whose CRM stage lags is anchored to the
    # Vendor-Selected floor (72) with the 100 ceiling unlocked. Triple-gated; ONLY RAISES.
    override = _selection_override(record, strengths)
    anchor_eff = max(anchor, SELECTION_FLOOR) if override else anchor
    ceiling_eff = 100.0 if override else ceiling
    if override:
        contributions.append(_contrib("selection_override", round(anchor_eff - anchor, 1),
                                      "confirmed selection (high preference, no rival ahead, defensible read) "
                                      "— anchored to the Vendor-Selected floor, full ceiling unlocked"))

    # §5 Forecast-conviction credit — an evidence-consistent Commit/Best Case adds to Win.
    fc_credit, fc_why = _forecast_conviction_credit(record)
    if fc_credit > 0:
        contributions.append(_contrib("forecast_conviction", round(fc_credit, 1), fc_why))

    # RISK OUT OF POSITION (2026-07-07 VP definition, user-ratified via Techtronic): Position
    # is a PURE win-likelihood read — "if it closes, do we win?" Risk lives in Deal Risk and
    # Forecast Confidence ("will it close?"), not here — charging it in Position double-counted
    # it (Techtronic: honest 47 'weak fundamentals' vs 33 'weak AND risky twice over').
    risk_pen = 0.0
    if deal_risk is not None and _num(deal_risk) and float(deal_risk) > WIN_RISK_NOISE_FLOOR:
        contributions.append(_contrib("risk_note", 0.0,
                                      f"deal risk {round(float(deal_risk))} is carried by Deal Risk / Forecast Confidence, not Position"))

    # OPPORTUNITY-TREND NUDGE (VP spec §4): CRM moves are buying/loss signals — stage/forecast
    # up-moves and amount growth nudge Win up; a close-date push (the SLIP signal lives HERE,
    # not in Momentum) and downgrades nudge it down. Modest, capped ±8.
    trend, trend_why = _trend_nudge(record)
    if trend:
        contributions.append(_contrib("trend_nudge", round(trend, 1), trend_why))

    # RELATIONSHIP LEVERAGE (+10): an expansion/phase-2 opp on an account we already run.
    rel_ok, rel_why, _rel_bm = _relationship_context(record)
    rel_pts = REL_WIN_PTS if rel_ok else 0.0
    if rel_pts:
        contributions.append(_contrib("relationship_leverage", rel_pts, rel_why))

    # Floor a LIVE deal at 5 — score_win_position only runs for live deals (dead/lost return
    # earlier with win 0), so win=0 would falsely read as "lost". 5 = "almost no chance, but
    # still live". Keeps the compounding downside (evidence + momentum drag + risk) honest
    # without colliding with the lost-deal sentinel.
    score = round(min(ceiling_eff, max(5.0, _clamp(anchor_eff + adj + mom_adj + scope_pts + fc_credit + trend + rel_pts - risk_pen, 0.0, 99.0))), 1)
    # Only-raise guard: the override must never yield a LOWER score than no-override.
    if override:
        base = round(min(ceiling, max(5.0, _clamp(anchor + adj + mom_adj + scope_pts + fc_credit + trend + rel_pts - risk_pen, 0.0, 99.0))), 1)
        score = max(score, base)
    return {"score": score, "baseline": round(anchor, 1), "anchor": round(anchor_eff, 1),
            "lift": adj, "ceiling": ceiling_eff, "momentum_adj": mom_adj,
            "scope_adj": scope_pts, "risk_penalty": -round(risk_pen, 1), "trend_nudge": round(trend, 1),
            "selection_override": bool(override), "contributions": contributions}


def _trend_nudge(record: dict):
    """VP spec §4: blend recent CRM moves into Win, capped ±8. Stage/forecast up-moves weigh
    more than amount/close. The close-date SLIP belongs here (Position), not in Momentum —
    Momentum only reacts beyond its 60d tolerance."""
    ai = (record or {}).get("ai") or {}
    ot = ai.get("opp_trends") or {}
    total, why = 0.0, []

    def _dirn(v):
        if isinstance(v, (int, float)):
            return 1 if v > 0.02 else (-1 if v < -0.02 else 0)
        s = str(v or "").lower()
        if any(t in s for t in ("up", "advanc", "upgrad", "raised", "increase", "pulled in", "pulled forward")):
            return 1
        if any(t in s for t in ("down", "regress", "downgrad", "cut", "pushed", "reduce", "decrease")):
            return -1
        return 0

    d = _dirn(ot.get("stage_trend"))
    if d:
        total += 4.0 * d; why.append("stage " + ("advanced" if d > 0 else "regressed"))
    d = _dirn(ot.get("forecast_trend") if ot.get("forecast_trend") is not None else ot.get("forecast_category_trend"))
    if d:
        total += 3.0 * d; why.append("forecast " + ("upgraded" if d > 0 else "downgraded"))
    d = _dirn(ot.get("amount_trend"))
    if d and not _scope_shrink(record)[0]:   # a scope-shrink amount cut is already charged −7
        total += 2.0 * d; why.append("amount " + ("up" if d > 0 else "cut"))
    cdt = ot.get("close_date_trend")
    if isinstance(cdt, (int, float)) and abs(cdt) > 0.02:
        det = str(ot.get("close_date_trend_detail") or "")
        m = re.search(r"(?:pushed|pulled)\s*(?:out|in|forward)?\s*(?:by\s*)?(\d+)\s*d", det, re.I)
        days = int(m.group(1)) if m else None
        if cdt > 0:
            total += 2.0; why.append("close pulled in")
        else:
            pen = min(4.0, ((days or 45) / 45.0) * 2.0)
            total -= pen; why.append(f"close pushed{f' {days}d' if days else ''}")
    total = max(-8.0, min(8.0, total))
    return round(total, 1), ("recent CRM moves: " + ", ".join(why)) if why else ""


def score_momentum(ev, dsl, expected):
    contributions, total = [], 50.0
    for k, (pol, w) in MOMENTUM.items():
        s = _get(ev, k)
        if s is None:
            continue
        mag = max(0.0, min(1.0, s.strength))
        pts = pol * mag * w
        total += pts
        contributions.append(_contrib(k, pts, s.evidence))
    pre = _clamp(total)
    note, score = None, pre
    if dsl is not None and dsl > MOMENTUM_WINDOW:
        # Silence DRAGS momentum down (a stalled enterprise deal is losing momentum, not
        # "flat"), assessed over a 30-60d window: only quiet beyond ~30d counts as stalling,
        # and the drag scales across the next 30 days, up to STALL_MAX.
        overdue = dsl - MOMENTUM_WINDOW
        stall = MOMENTUM_STALL_MAX * (1.0 - exp(-overdue / MOMENTUM_STALL_TAU))
        score = pre - stall
        note = f"{int(dsl)}d quiet (>30d window); momentum down {stall:.0f}pt (stalling)"
    return {"score": round(_clamp(score), 1), "pre_decay": round(pre, 1),
            "decay_note": note, "contributions": contributions}


def score_commitment(ev):
    contributions, raw = [], 0.0
    for k, w in COMMITMENT.items():
        s = _get(ev, k)
        if s is None:
            continue
        mag = max(0.0, min(1.0, s.strength))
        raw += mag * w
        contributions.append((k, mag * w, s.evidence))
    score = COMMITMENT_FLOOR + _saturate(raw, 100.0 - COMMITMENT_FLOOR, COMMITMENT_SCALE)
    out = []
    if raw > 0:
        gained = score - COMMITMENT_FLOOR
        for k, pts, evi in contributions:
            out.append(_contrib(k, gained * pts / raw, evi))
    return {"score": round(_clamp(score), 1), "contributions": out}


def score_risk(ev):
    contributions, raw = [], 0.0
    for k, w in RISK.items():
        s = _get(ev, k)
        if s is None:
            continue
        mag = max(0.0, min(1.0, s.strength))
        raw += mag * w
        contributions.append((k, mag * w, s.evidence))
    score = _saturate(raw, 100.0, RISK_SCALE)
    out = []
    if raw > 0:
        for k, pts, evi in contributions:
            out.append(_contrib(k, score * pts / raw, evi))
    return {"score": round(_clamp(score), 1), "contributions": out}


def score_coverage(ev, dsl, expected):
    dims = [d for d, fs in READ_DIMENSIONS.items() if any(_get(ev, f) is not None for f in fs)]
    breadth = len(dims) / len(READ_DIMENSIONS)
    recency = 1.0
    if dsl is not None and dsl > expected:
        recency = exp(-(dsl - expected) / (MOMENTUM_DECAY_TAU * 2))
    cov = _clamp(100.0 * breadth * recency)
    label = next(name for thr, name in COVERAGE_BANDS if cov >= thr)
    return {"score": round(cov, 1), "label": label, "dimensions_read": dims,
            "dimensions_total": len(READ_DIMENSIONS), "recency_factor": round(recency, 2)}


# FC = forecast confidence = odds this closes in the forecast window. ANCHOR ON WIN (the
# stage-anchored close probability), then adjust by execution signals: commitment and
# momentum above/below neutral nudge it, risk drags it. Coverage is a CONFIDENCE FLAG, not
# a multiplier — a thin read means we know less, not that the deal is less likely to close.
# Calibrated 2026-06-29 so a clean Commit-in-contracting lands 90+ (user-approved ceiling).
FC_COM_W = 0.20
FC_MOM_W = 0.12
FC_RISK_W = 0.50


def score_forecast_confidence(win, mom, com, rsk, cov):
    fc = win + FC_COM_W * (com - 50.0) + FC_MOM_W * (mom - 50.0) - FC_RISK_W * rsk
    fc = _clamp(fc, 0.0, 99.0)
    return {"score": round(fc, 1), "core": round(fc, 1),
            "coverage": round(cov, 1),
            "coverage_flag": "partial" if cov < 60 else "full"}


# ----------------------------------------------------------------------------
# Hybrid factor derivation from the swept record
# ----------------------------------------------------------------------------
def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _status_on(s):
    s = str(s or "").strip().lower()
    return s in ("identified", "strong", "confirmed", "active", "yes", "met", "controlled", "engaged")


def derive_evidence(record: dict):
    """Map the swept record's structured signals -> factor Signals + cadence.
    Defensive throughout; any missing field simply leaves its factor unobserved."""
    hard = record.get("hard") or {}
    ai = record.get("ai") or {}
    pulse = record.get("pulse") or {}
    packets = record.get("packets") or []
    vh = record.get("verdict_history") or []

    nv = ai.get("north_star_verdict") or {}
    verdict = str(nv.get("verdict") or "")
    trajectory = str(nv.get("trajectory") or "").lower()
    defensible = bool(nv.get("forecast_defensible"))
    state = str(pulse.get("state") or "").lower()
    dsl = pulse.get("days_since_activity")
    dsq = pulse.get("days_since_qualified")
    rep_push = bool((pulse.get("rep_outreach") or {}).get("detected"))
    buyer_calls = bool(pulse.get("buyer_calls_seen"))
    stage = str(hard.get("stage") or "").lower()

    ev: dict = {}

    def put(key, strength, text, structural=False):
        if key in SIGNED_KEYS:
            strength = max(-1.0, min(1.0, strength))
        else:
            strength = max(0.0, min(1.0, strength)) * _recency_weight(dsl, structural)
            if strength <= 0:
                return
        ev[key] = Signal(round(strength, 3), text)

    # --- pain / fit (structural) ---
    fit = str(ai.get("ai_fit_signal") or "").upper()
    if fit == "HIGH":
        put("pain_fit", 0.7, "Strong product/pain fit signal from the sweep.", structural=True)
    elif fit == "MID":
        put("pain_fit", 0.4, "Moderate product/pain fit signal from the sweep.", structural=True)
    elif fit == "LOW":
        ev["pain_fit"] = Signal(-0.3, "Weak/off-target fit signal from the sweep.")

    # --- engagement direction (buyer pulling vs rep pushing) ---
    if state == "live" and buyer_calls:
        put("engagement_direction", 0.5, "Pulse live with buyer calls read this sweep.")
    elif state == "cooling":
        put("engagement_direction", -0.2, "Pulse cooling; engagement softening.")
    elif state == "dark":
        ev["engagement_direction"] = Signal(-0.4, "Pulse dark; no recent buyer engagement.")
    elif rep_push and not buyer_calls:
        ev["engagement_direction"] = Signal(-0.3, "Rep is doing the outreach; no buyer-side pull.")

    # --- stage / evidence alignment + inflation ---
    cov = ai.get("evidence_coverage") or {}
    gaps = cov.get("gaps") or ai.get("gaps") or []
    advanced_stage = any(s in stage for s in ("shortlist", "vendor selected", "contract", "negotiat"))
    if defensible:
        put("stage_evidence_alignment", 0.5, "Verdict marks the forecast defensible against the stage.")
    elif advanced_stage and (state == "dark" or not buyer_calls):
        ev["stage_evidence_alignment"] = Signal(-0.4, "Advanced stage not supported by recent engagement.")
        put("stage_inflation", 0.5, "Stage is ahead of the engagement/evidence the sweep found.")

    # --- competitive posture ---
    # Only a BUYER-LEANING competitor (preferred / ahead / down-selected) is a negative
    # posture; a credible rival merely present in the eval is a neutral "open competitive
    # RFP" signal, NOT a win penalty (threat_level alone never triggers the drag).
    comp = ai.get("competitive_position") or {}
    citems = comp.get("items") or []
    leaning = [c for c in citems if _buyer_leans_competitor(c)]
    if leaning:
        ev["competitive_posture"] = Signal(-0.4, f"Buyer is leaning toward a competitor ({len(leaning)} flagged).")
        put("competitor_preferred", 0.5, "Sweep flags the buyer leaning toward / preferring a competitor.")
    elif citems:
        put("open_competitive_rfp", 0.5, "Active competitive evaluation with named rivals (no buyer leaning).")

    # --- exec / champion / stakeholders (MEDDPICC + packets) ---
    medd = ai.get("meddpicc") or {}
    eb = (medd.get("economic_buyer") or {}).get("status")
    ch = (medd.get("champion") or {}).get("status")
    if _status_on(eb):
        put("exec_access", 0.7, "Economic buyer identified/engaged per MEDDPICC.")
        put("exec_access_granted", 0.6, "Direct economic-buyer access recorded.")
    champ_pkts = [p for p in packets if str(p.get("type")) == "champion"
                  and str(p.get("status") or "active") not in ("retired", "resolved", "superseded")]
    if _status_on(ch) or champ_pkts:
        strength = 0.7 if _status_on(ch) else 0.5
        put("champion_strength", strength, "A named champion is active on the deal.")
    smap = (ai.get("stakeholder_map") or {}).get("items") or []
    if len(smap) >= 3:
        put("stakeholder_expansion", min(0.3 + 0.1 * (len(smap) - 3), 0.8),
            f"{len(smap)} stakeholders engaged across the account.")

    # --- commercial motion + commitment (packets / MEDDPICC) ---
    paper = (medd.get("paper_process") or {}).get("status")
    has_pricing = any(str(p.get("type")) in ("commitment", "requirement")
                      and "pric" in str(p.get("subject", "")).lower() for p in packets)
    if _status_on(paper) or has_pricing:
        put("commercial_motion", 0.6, "Pricing/paper/procurement motion is live.")
    sec = any("security" in str(p.get("subject", "")).lower() or "procurement" in str(p.get("subject", "")).lower()
              for p in packets if str(p.get("type")) in ("requirement", "commitment"))
    if sec:
        put("security_or_procurement_review", 0.6, "Security/procurement review underway.")
    cust_commit = [p for p in packets if str(p.get("type")) == "commitment"
                   and str(p.get("status") or "") not in ("resolved", "superseded")]
    if cust_commit:
        put("customer_action_items", min(0.4 + 0.1 * len(cust_commit), 0.8),
            f"Customer owns {len(cust_commit)} open commitment(s).")
        put("internal_process_shared", 0.5, "Customer shared internal process / commitments.")

    # --- momentum (trajectory + pulse + verdict) ---
    if trajectory in ("stronger", "up"):
        put("customer_action_items_increasing", 0.5, "Verdict trajectory strengthening this sweep.")
        put("stage_advanced_with_evidence", 0.4, "Forward motion with supporting evidence.")
    elif trajectory in ("weaker", "down"):
        put("customer_passivity", 0.5, "Verdict trajectory weakening this sweep.")
    if state in ("cooling", "dark"):
        put("attendance_or_cadence_drop", 0.5 if state == "dark" else 0.35,
            f"Engagement cadence dropped (pulse {state}).")
    if rep_push and not buyer_calls:
        put("customer_passivity", 0.4, "Rep driving cadence; customer not initiating.")
    if state == "live" and buyer_calls:
        put("buyer_engaged_this_sweep", 0.6, "Buyer actively engaged this sweep (live pulse + buyer calls).")

    # --- granular RUBRIC momentum signals (2026-06-29) ---
    # These are CALL-LEVEL, TIME-SENSITIVE signals (a senior joined THIS period, the customer
    # asked for pricing, praised a competitor, named specific dates). They cannot be faked
    # from static MEDDPICC status without inflating stalled deals, so they fire ONLY from the
    # sweep's extracted signals (ai.momentum_signals.<key>); dormant until the sweep emits
    # them (see the "extract transcript-only signals" sweep-prompt change). Each value may be
    # a bool (-> 0.6) or a 0-1 strength.
    msig = ai.get("momentum_signals") if isinstance(ai.get("momentum_signals"), dict) else {}
    _MOM_GRANULAR = ("seniority_rising", "commercial_topics_entering", "concrete_dates",
                     "customer_requested_next_meeting", "close_plan_concretizing",
                     "generic_demo_only", "competitor_praised")
    for _k in _MOM_GRANULAR:
        _v = msig.get(_k)
        if isinstance(_v, bool):
            if _v:
                put(_k, 0.6, f"{_k.replace('_', ' ')} (from call evidence).")
        elif isinstance(_v, (int, float)):
            put(_k, max(0.0, min(1.0, float(_v))), f"{_k.replace('_', ' ')} (from call evidence).")

    # --- close-date direction & risk ---
    # A close date pulled FORWARD (opp_trends.close_date_trend > 0) is the buyer
    # ACCELERATING — credit it as positive momentum, and do NOT let the date-risk
    # negative fire against it. (Old bug: a forward pull was penalised as a "push",
    # so a deal accelerating its own date LOST momentum — e.g. HAVI.)
    ct = _num((ai.get("opp_trends") or {}).get("close_date_trend"))
    ct_detail = str((ai.get("opp_trends") or {}).get("close_date_trend_detail") or "")
    pulled_forward = ct is not None and ct > 0
    if pulled_forward:
        put("close_date_pulled_forward", min(1.0, ct),
            ct_detail or "Close date pulled forward — buyer accelerating.")
    cdr_now = verdict == "Close Date Risk"
    cdr_count = sum(1 for h in vh if str(h.get("verdict")) == "Close Date Risk")
    if cdr_now and not pulled_forward:
        put("close_date_pushed", 0.5, "Verdict flags close-date risk this sweep.")
    if cdr_count >= 2 and not pulled_forward:
        put("close_date_pushed_repeatedly", min(0.4 + 0.1 * cdr_count, 0.8),
            f"Close-date risk recurred across {cdr_count} sweeps.")
    # --- Next Step actively worked: dated milestones in the running Next_Step log ---
    ns_dates = _count_dated_milestones(hard.get("next_step"))
    if ns_dates >= 1:
        put("next_step_active", min(0.4 + 0.2 * ns_dates, 0.9),
            f"Next Step carries {ns_dates} dated milestone(s) — actively worked.")
    # An overdue / imminent close date is ITSELF a close-date risk, independent of the
    # verdict wording — fixes deals (e.g. Mair) whose date has passed reading 0 risk.
    dtc = _num(pulse.get("days_to_close"))
    signed = any(t in stage for t in ("signed", "po received", "po-received", "won", "closed"))
    if dtc is not None and not signed:
        if dtc < 0:
            put("close_date_pushed_repeatedly", min(0.5 + (-dtc) / 120.0, 0.9),
                f"Close date passed {int(-dtc)}d ago without signature.")
        elif dtc <= 14 and advanced_stage:
            put("close_date_pushed_repeatedly", 0.45,
                f"Close date in {int(dtc)}d with signature not yet secured.")

    # --- stage stuck past cadence ---
    if dsq is not None and dsq > 120 and state in ("cooling", "dark"):
        put("stage_stuck_past_cadence", 0.5, f"Sat in-stage ~{dsq}d with cadence dropping.")

    # --- verdict-driven risk overlay ---
    if verdict in ("Off Track", "At Risk"):
        put("low_buyer_intent", 0.5, f"Verdict '{verdict}' — weak forward intent.")
    if verdict == "Slowing":
        put("customer_passivity", 0.4, "Verdict 'Slowing' — momentum stalling.")

    cadence = {"days_since_last_call": dsl, "expected_cadence_days": 14}
    return ev, cadence


def _overlay_agent_factors(ev: dict, record: dict):
    """If the sweep agent emitted soft judgment factors, overlay them (agent wins
    on the keys it provides). Tolerates the same JSON contract as the offline model."""
    dse = (record.get("ai") or {}).get("deal_scores_evidence") or {}
    factors = dse.get("factors") or {}
    if not isinstance(factors, dict):
        return ev, (dse.get("cadence") or {})
    for k, v in factors.items():
        if k not in ALL_KEYS or not isinstance(v, dict):
            continue
        st = _num(v.get("strength"))
        if st is None:
            continue
        if k in SIGNED_KEYS:
            st = max(-1.0, min(1.0, st))
        else:
            st = max(0.0, min(1.0, st))
            if st <= 0:
                continue
        ev[k] = Signal(round(st, 3), str(v.get("evidence") or "")[:300])
    return ev, (dse.get("cadence") or {})


# ----------------------------------------------------------------------------
# Commentary (2 sentences per score, grounded in the firing factors)
# ----------------------------------------------------------------------------
def _trim(s, n=140):
    s = (s or "").strip().rstrip(".")
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"


def _pn(contribs):
    pos = sorted([c for c in contribs if c["points"] > 0], key=lambda c: -c["points"])
    neg = sorted([c for c in contribs if c["points"] < 0], key=lambda c: c["points"])
    return pos, neg


def _commentary(win, mom, com, rsk, fc, cov, h):
    out = {}
    pos, neg = _pn(win["contributions"])
    if not win["contributions"]:
        out["win_position"] = ("Win sits at the neutral 50 prior — only the most basic read was observable, "
                               "so winnability is neither supported nor contradicted yet.")
    else:
        lead = "; ".join(_trim(c["evidence"], 100) for c in pos[:2]) if pos else _trim(neg[0]["evidence"])
        s1 = f"Win {win['score']:.0f} is carried by {lead}." if pos else f"Win {win['score']:.0f} reflects a weak position: {lead}."
        s2 = (f"Offsetting it: {_trim(neg[0]['evidence'])}." if neg
              else "No countervailing weakness surfaced in the swept evidence.")
        out["win_position"] = s1 + " " + s2
    pos, neg = _pn(mom["contributions"])
    band = "moving forward" if mom["score"] > 55 else ("roughly flat" if mom["score"] >= 45 else "slipping backward")
    if not mom["contributions"]:
        out["deal_momentum"] = f"Momentum is flat at {mom['score']:.0f}: no forward or backward motion was observed."
    else:
        drv = _trim(pos[0]["evidence"]) if (mom["score"] >= 50 and pos) else (_trim(neg[0]["evidence"]) if neg else "")
        s2 = (f"Eased toward flat — {_trim(mom['decay_note'])}." if mom.get("decay_note")
              else ("Silence alone was not treated as backward motion."))
        out["deal_momentum"] = f"Momentum {mom['score']:.0f} ({band}): {drv}. {s2}"
    pos, _ = _pn(com["contributions"])
    if com["score"] <= 12 or not pos:
        out["customer_commitment"] = ("Commitment sits near the earned-from-zero floor: little observable customer "
                                      "investment surfaced. Commitment is earned, so a low value is honest, not a penalty.")
    else:
        out["customer_commitment"] = (f"Commitment {com['score']:.0f} reflects real customer investment: "
                                      f"{'; '.join(_trim(c['evidence'], 100) for c in pos[:2])}.")
    pos, _ = _pn(rsk["contributions"])
    if rsk["score"] == 0 or not pos:
        caveat = " Coverage is thin, so read this as 'nothing seen yet,' not 'nothing there.'" if h["read"] in ("Early Read", "Partial Read") else ""
        out["deal_risk"] = f"Risk 0: no break-risk was observed in the swept evidence.{caveat}"
    else:
        out["deal_risk"] = (f"Risk {rsk['score']:.0f} is driven by {'; '.join(_trim(c['evidence'], 100) for c in pos[:2])}. "
                            "Only observed negatives count, so these are real warning signs, not gaps.")
    s2 = (f" Read is {h['read']} — a confidence flag (we know less), not a haircut on the score."
          if fc.get("coverage_flag") == "partial" else "")
    out["forecast_confidence"] = (f"Forecast confidence {fc['score']:.0f} is anchored on the stage win "
                                  f"({h['win_position']:.0f}), nudged by commitment {h['customer_commitment']:.0f} "
                                  f"and momentum {h['deal_momentum']:.0f}, and dragged by risk {h['deal_risk']:.0f}." + s2)
    dims = ", ".join(d.replace("_", " ") for d in cov["dimensions_read"]) or "none"
    s2 = (f"Reduced because the most recent contact is stale (recency ×{cov['recency_factor']:.2f})."
          if cov.get("recency_factor", 1) < 1 else "It labels how much of the picture we have, never the primary scores.")
    out["evidence_coverage"] = f"{cov['label']}: {len(cov['dimensions_read'])} of {cov['dimensions_total']} evidence dimensions seen ({dims}). " + s2
    return out


# ----------------------------------------------------------------------------
# Top-level entry — guarded, never raises
# ----------------------------------------------------------------------------
# Stage-aware risk: once a deal is executing its contract (LATE), only close-date /
# budget factors are legitimate risks — competitor / passivity / access / stage-
# inflation etc. are early/mid concerns and must not inflate a contracting deal's
# risk score (mirrors the stage-aware verdict rules in the sweep prompt).
_LATE_RISK_OK = {"close_date_pushed_repeatedly", "budget_frozen_or_unclear"}
# A live multi-vendor fight at contracting is still a real loss risk; merely having
# named rivals on file is not. derive_evidence fires `competitor_preferred` ONLY when a
# competitor is flagged ahead / incumbent / high-threat (a real fight) and
# `open_competitive_rfp` for plain "named rivals exist". So at LATE we re-admit the
# former and keep dropping the latter. (Competition strength is a fixed 0.5 today — there
# is no recency decay yet — so the gate is by-factor, with a 0.5 floor, not a high cutoff.)
_LATE_COMPETE = {"competitor_preferred"}
_LATE_COMPETE_MIN = 0.5


def _late_keep_risk(k, sig) -> bool:
    """At LATE, keep a risk factor if it's close-date/budget, or a competition factor
    whose signal is strong enough to be a live dogfight (not a stale mention)."""
    if k not in RISK:
        return True
    if k in _LATE_RISK_OK:
        return True
    if k in _LATE_COMPETE and isinstance(sig, Signal) and float(sig.strength or 0.0) >= _LATE_COMPETE_MIN:
        return True
    return False


def _stage_tier(record: dict) -> str:
    s = str(((record.get("hard") or {}).get("stage")) or "").lower()
    if "contract" in s or "po received" in s or "po-received" in s:
        return "late"
    if "shortlist" in s or "vendor select" in s or s.strip() == "selected":
        return "mid"
    return "early"


# A DEAD deal (lost / qualified out / omitted) is no longer a live opportunity: stop all
# selling action items, scores, and forecast roll-up. Detected from EITHER the stage OR the
# forecast category (one field killing it is enough). Closed WON is NOT dead here — it's a
# different motion (handoff), out of scope for this suppression. Re-opening a deal (stage
# back to a live value) auto-revives it, because everything here is computed read-time.
_DEAD_STAGE_MARKERS = ("closed lost", "qualified out", "closed-lost", "qualified-out")


def is_dead_deal(record: dict):
    """Return a label ('Lost' | 'Omitted') if the deal is dead, else None."""
    hard = (record or {}).get("hard") or {}
    stage = str(hard.get("stage") or "").lower()
    fc = str(hard.get("forecast_category") or "").lower()
    if "qualified out" in stage or "qualified-out" in stage:
        return "Qualified Out"
    if "closed lost" in stage or "closed-lost" in stage or stage.strip() == "lost":
        return "Lost"
    if fc == "omitted":
        return "Omitted"
    return None


def compute_deal_scores(record: dict) -> dict:
    """Return the deal_scores block for one swept record. Never raises."""
    if not ENABLED:
        return {}
    # HARD OVERRIDE — an explicit LOSS detected in the latest call/notes/Next-Step ends the
    # deal NOW, regardless of CRM stage or how much activity preceded it. A lost deal must
    # read Win 0 / Momentum 0 instantly — never "healthy" off stale engagement (the HAVI case:
    # lost to Coupa on the Jun-29 call while SF still showed Shortlisted / Upside Key Deal).
    dec = (record.get("ai") or {}).get("decision_outcome") or {}
    if dec.get("status") == "lost":
        src = dec.get("source") or "the latest call"
        ev_txt = (dec.get("evidence") or "").strip()
        why = (f"We lost this deal — detected in {src}"
               + (f": \"…{ev_txt[:160]}…\"" if ev_txt else "")
               + ". Win and Momentum are 0; activity before the decision no longer counts.")
        return {"schema_version": SCHEMA_VERSION, "decision": "lost",
                "dead": True, "dead_label": f"Lost ({src})",
                "headline": {"win_position": 0, "deal_momentum": 0, "customer_commitment": 0,
                             "deal_risk": 100, "forecast_confidence": 0, "read": "Lost",
                             "dead": True, "dead_label": "Lost",
                             "decision": "lost", "decision_source": src},
                "commentary": {k: why for k in ("win_position", "deal_momentum",
                               "customer_commitment", "deal_risk", "forecast_confidence")}}
    dead = is_dead_deal(record)
    if dead:
        # No live scores for a dead deal — surface a terminal state instead of misleading
        # numbers (a lost deal must not read win 40 / FC 34).
        return {"schema_version": SCHEMA_VERSION, "dead": True, "dead_label": dead,
                "headline": {"dead": True, "dead_label": dead, "read": dead,
                             "win_position": None, "deal_momentum": None,
                             "customer_commitment": None, "deal_risk": None,
                             "forecast_confidence": None}}
    try:
        ev, cadence = derive_evidence(record)
        ev, agent_cadence = _overlay_agent_factors(ev, record)
        dsl = agent_cadence.get("days_since_last_call", cadence.get("days_since_last_call"))
        expected = int(agent_cadence.get("expected_cadence_days") or cadence.get("expected_cadence_days") or 14)
        dsl = None if dsl is None else int(dsl)

        # Momentum FIRST — it feeds Win's momentum-drag, so a high-stage deal that isn't
        # behaving like its stage demands (low momentum) falls instead of riding the anchor.
        # ONE ENGINE (2026-07-07, VP spec ratified): ALWAYS the engagement/process model.
        # The old footprints-gate silently routed every null-footprint deal (Techtronic!)
        # to the legacy signal model — bypassing process-mode, the stretched RFP clock,
        # next-step plan terms, stalling cadence, close tolerance… the entire spec.
        # score_momentum_v2 handles missing footprints itself (legacy volume fallback,
        # process-mode floor, next-step terms from the hard record).
        mom = score_momentum_v2(record)
        # Risk BEFORE Win — Risk is folded into Win (spec §5: high risk penalises winnability).
        # Stage-bound the risk: at LATE (contract executing) only close-date / budget factors
        # count — strip early/mid ones so they can't inflate it. Exception: a LIVE multi-vendor
        # fight (strong, fresh competition) is still a real loss risk at contracting.
        ev_risk = ev
        if _stage_tier(record) == "late":
            ev_risk = {k: v for k, v in ev.items() if _late_keep_risk(k, v)}
        rsk = score_risk(ev_risk)
        win = score_win_position(ev, record, momentum=mom.get("score"), deal_risk=rsk.get("score"))
        com = score_commitment(ev)
        cov = score_coverage(ev, dsl, expected)
        fc = score_forecast_confidence(win["score"], mom["score"], com["score"], rsk["score"], cov["score"])
        headline = {"win_position": win["score"], "deal_momentum": mom["score"],
                    "customer_commitment": com["score"], "deal_risk": rsk["score"],
                    "forecast_confidence": fc["score"], "read": cov["label"]}
        return {
            "schema_version": SCHEMA_VERSION,
            "headline": headline,
            "win_position": win, "deal_momentum": mom, "customer_commitment": com,
            "deal_risk": rsk, "forecast_confidence": fc, "evidence_coverage": cov,
            "commentary": _commentary(win, mom, com, rsk, fc, cov, headline),
            "factor_source": "hybrid",
        }
    except Exception as e:  # never break a sweep over scoring
        return {"schema_version": SCHEMA_VERSION, "error": f"scoring_failed: {e}"}


if __name__ == "__main__":  # quick self-test of the arithmetic against reference cases
    import json
    thin = {"hard": {"stage": "Qualified"},
            "pulse": {"state": "live", "days_since_activity": 4, "buyer_calls_seen": True},
            "ai": {"ai_fit_signal": "HIGH", "north_star_verdict": {"verdict": "On Track", "forecast_defensible": True}}}
    print(json.dumps(compute_deal_scores(thin)["headline"], indent=2))
