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
from dataclasses import dataclass
from math import exp
from typing import Optional

ENABLED = os.getenv("DEAL_SCORES_ENABLED", "1").strip().lower() not in ("0", "false", "no", "")
SCHEMA_VERSION = 1


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
}
MOMENTUM_DECAY_TAU = 14.0
MOMENTUM_STALL_MAX = 25.0   # a deal far past cadence loses up to this much momentum (sinks below 50)

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
    "forward_motion": ["customer_requested_next_meeting", "customer_next_meeting_request", "next_meeting_declined", "concrete_dates", "close_date_pushed", "customer_action_items_increasing", "commercial_topics_entering"],
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
WIN_BAND = 15.0                # max points signals can move a deal off its stage anchor
WIN_HEALTH_SCALE = 4.0        # net signal score that maps to a full +/-band
# Within-stage POSITIVE drivers (your list: product fit, buyer momentum, we're leading /
# champion + EB access, milestone success, pricing/commercial comfort, multi-threading).
# pain_fit / engagement_direction are SIGNED (can pull down); the rest are magnitude-only.
WIN_POS = {"pain_fit": 1.0, "engagement_direction": 1.0, "champion_strength": 1.0,
           "exec_access": 1.0, "commercial_motion": 0.9, "stage_advanced_with_evidence": 0.8,
           "customer_action_items": 0.6, "stakeholder_expansion": 0.5}
# Within-stage LOSS risk (drags win): a competitor ahead, no-decision drift, stage bluff.
# NOTE: close-date / budget / paperwork are TIMING risks — they do NOT drag win (you still
# win the deal, just later); they live in deal_risk / momentum instead.
WIN_NEG = {"competitor_preferred": 1.2, "open_competitive_rfp": 0.5,
           "low_buyer_intent": 1.0, "customer_passivity": 0.8, "stage_inflation": 0.8}


def _win_anchor(record: dict) -> float:
    s = str(((record or {}).get("hard") or {}).get("stage") or "").lower()
    for sub, prior in WIN_STAGE_ANCHOR:
        if sub in s:
            return float(prior)
    return WIN_ANCHOR_DEFAULT


def score_win_position(ev, record=None):
    anchor = _win_anchor(record)
    contributions, health = [], 0.0
    for k, w in WIN_POS.items():
        s = _get(ev, k)
        if s is None:
            continue
        v = max(-1.0, min(1.0, s.strength)) * w   # signed for fit/engagement, + for the rest
        health += v
        if abs(v) >= 0.05:
            contributions.append(_contrib(k, v, s.evidence))
    for k, w in WIN_NEG.items():
        s = _get(ev, k)
        if s is None:
            continue
        mag = max(0.0, min(1.0, s.strength)) * w
        health -= mag
        if mag >= 0.05:
            contributions.append(_contrib(k, -mag, s.evidence))
    health_n = max(-1.0, min(1.0, health / WIN_HEALTH_SCALE))
    adj = round(WIN_BAND * health_n, 1)
    score = round(_clamp(anchor + adj, 0.0, 99.0), 1)
    return {"score": score, "baseline": round(anchor, 1), "anchor": round(anchor, 1),
            "lift": adj, "contributions": contributions}


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
    if dsl is not None and dsl > expected:
        # Silence DRAGS momentum down (a stalled enterprise deal is losing momentum, not
        # "flat"). The drag grows with how far past cadence the deal is, up to STALL_MAX.
        overdue = dsl - expected
        stall = MOMENTUM_STALL_MAX * (1.0 - exp(-overdue / MOMENTUM_DECAY_TAU))
        score = pre - stall
        note = f"{overdue}d past expected cadence; momentum down {stall:.0f}pt (stalling)"
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
    comp = ai.get("competitive_position") or {}
    citems = comp.get("items") or []
    ahead = [c for c in citems if str(c.get("threat_level") or "").lower() in ("high", "critical")
             or str(c.get("status") or "").lower() in ("preferred", "ahead", "incumbent")]
    if ahead:
        ev["competitive_posture"] = Signal(-0.4, f"A competitor is ahead/incumbent ({len(ahead)} flagged).")
        put("competitor_preferred", 0.5, "Sweep flags a competitor as preferred/incumbent with momentum.")
    elif citems:
        put("open_competitive_rfp", 0.5, "Active competitive evaluation with named rivals.")

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

    # --- close-date risk from verdict + history ---
    cdr_now = verdict == "Close Date Risk"
    cdr_count = sum(1 for h in vh if str(h.get("verdict")) == "Close Date Risk")
    if cdr_now:
        put("close_date_pushed", 0.5, "Verdict flags close-date risk this sweep.")
    if cdr_count >= 2:
        put("close_date_pushed_repeatedly", min(0.4 + 0.1 * cdr_count, 0.8),
            f"Close-date risk recurred across {cdr_count} sweeps.")
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


def compute_deal_scores(record: dict) -> dict:
    """Return the deal_scores block for one swept record. Never raises."""
    if not ENABLED:
        return {}
    try:
        ev, cadence = derive_evidence(record)
        ev, agent_cadence = _overlay_agent_factors(ev, record)
        dsl = agent_cadence.get("days_since_last_call", cadence.get("days_since_last_call"))
        expected = int(agent_cadence.get("expected_cadence_days") or cadence.get("expected_cadence_days") or 14)
        dsl = None if dsl is None else int(dsl)

        win = score_win_position(ev, record)
        mom = score_momentum(ev, dsl, expected)
        com = score_commitment(ev)
        # Stage-bound the risk: at LATE (contract executing) only close-date / budget
        # risk factors count — strip the early/mid ones so they can't inflate it.
        # Exception: a LIVE multi-vendor fight (strong, fresh competition) is still a
        # real loss risk at contracting, so re-admit strong competition signals.
        ev_risk = ev
        if _stage_tier(record) == "late":
            ev_risk = {k: v for k, v in ev.items() if _late_keep_risk(k, v)}
        rsk = score_risk(ev_risk)
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
