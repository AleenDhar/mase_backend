"""Tests for deal_engine_scoring — arithmetic fidelity, safety, derive + overlay."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deal_engine_scoring as m  # noqa: E402

S = m.Signal


def _score(ev, dsl, exp):
    win = m.score_win_position(ev)
    mom = m.score_momentum(ev, dsl, exp)
    com = m.score_commitment(ev)
    rsk = m.score_risk(ev)
    cov = m.score_coverage(ev, dsl, exp)
    fc = m.score_forecast_confidence(win["score"], mom["score"], com["score"], rsk["score"], cov["score"])
    return win, mom, com, rsk, cov, fc


def test_arithmetic_matches_reference_rich_healthy():
    ev = {"pain_fit": S(0.9), "engagement_direction": S(0.8), "stage_evidence_alignment": S(0.7),
          "competitive_posture": S(0.6), "exec_access": S(0.9), "champion_strength": S(0.8),
          "commercial_motion": S(0.8), "customer_action_items": S(0.7), "stakeholder_expansion": S(0.6),
          "customer_action_items_increasing": S(0.7), "commercial_topics_entering": S(0.8),
          "customer_requested_next_meeting": S(0.7), "seniority_rising": S(0.6),
          "internal_process_shared": S(0.7), "exec_access_granted": S(0.9),
          "security_or_procurement_review": S(0.8), "customer_next_meeting_request": S(0.7)}
    win, mom, com, rsk, cov, fc = _score(ev, 3, 10)
    assert win["score"] == 100.0
    assert mom["score"] == 73.7
    assert com["score"] == 75.3
    assert rsk["score"] == 0.0
    assert fc["score"] == 88.6
    assert cov["label"] == "Full Read"


def test_arithmetic_matches_reference_fat_rotten():
    ev = {"pain_fit": S(0.3), "engagement_direction": S(-0.7), "stage_evidence_alignment": S(-0.8),
          "stage_inflation": S(0.8), "customer_passivity": S(0.7), "low_buyer_intent": S(0.6)}
    win, mom, com, rsk, cov, fc = _score(ev, 9, 14)
    assert win["score"] == 40.2
    assert rsk["score"] == 57.3
    assert fc["score"] == 24.0
    assert cov["label"] == "Early Read"


def test_thin_deal_not_penalised():
    """One good baseline signal -> Win near baseline, Early Read, never cratered."""
    ev = {"pain_fit": S(0.7)}
    win, mom, com, rsk, cov, fc = _score(ev, 4, 14)
    assert win["score"] > 55  # not cratered
    assert rsk["score"] == 0.0  # absence is not risk
    assert cov["label"] == "Early Read"


def test_compute_never_raises_on_garbage():
    for bad in [None, {}, {"ai": None}, {"hard": 5}, {"ai": {"meddpicc": "x"}, "pulse": []}]:
        out = m.compute_deal_scores(bad if isinstance(bad, dict) else {})
        assert isinstance(out, dict)


def test_derive_from_record_live_healthy():
    rec = {"hard": {"stage": "Shortlisted"},
           "pulse": {"state": "live", "days_since_activity": 5, "buyer_calls_seen": True, "days_since_qualified": 60},
           "ai": {"ai_fit_signal": "HIGH",
                  "north_star_verdict": {"verdict": "On Track", "forecast_defensible": True, "trajectory": "stronger"},
                  "meddpicc": {"economic_buyer": {"status": "identified"}, "champion": {"status": "strong"}},
                  "stakeholder_map": {"items": [1, 2, 3, 4]}}}
    out = m.compute_deal_scores(rec)
    h = out["headline"]
    assert h["win_position"] > 60
    assert h["deal_momentum"] >= 50
    assert out["factor_source"] == "hybrid"
    assert "win_position" in out["commentary"]


def test_agent_overlay_wins():
    rec = {"hard": {"stage": "Qualified"}, "pulse": {"state": "dark", "days_since_activity": 30},
           "ai": {"deal_scores_evidence": {"cadence": {"days_since_last_call": 3, "expected_cadence_days": 10},
                                           "factors": {"pain_fit": {"strength": 0.9, "evidence": "agent says strong"}}}}}
    out = m.compute_deal_scores(rec)
    # agent pain_fit 0.9 should lift win baseline well above 50
    assert out["headline"]["win_position"] > 55


def test_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(m, "ENABLED", False)
    assert m.compute_deal_scores({"hard": {}}) == {}
