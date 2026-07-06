"""Guards the reason-quality / scoring tweaks (2026-07 deal-quality pass):
- CRO bullets weave the deal-specific SECTION NARRATIVE (not a bare label);
- the top risks are folded INTO the win block;
- an explicit weak/negative read survives the CRM keyword overlay (score↔reasons align);
- exec_access still lifts from a real structured EB field;
- scope-shrink drags Win and raises a CEO watch;
- second-panel expansion floors exec_access.
Pure/offline — no network, no LLM.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import deal_engine_scoring as SC
import deal_engine_cro as CRO
import deal_engine_ceo as CEO


def _rich_record():
    return {
        "hard": {"stage": "Shortlisted", "amount": 200000, "close_date": "2026-08-01",
                 "account_name": "Acme", "next_step": "Jul 10 review"},
        "pulse": {"state": "live", "days_since_activity": 5, "buyer_calls_seen": True},
        "ai": {"ai_fit_signal": {"tier": "AI Hungry", "summary": "Strong AI appetite; Merlin lands well."},
               "north_star_verdict": {"verdict": "On Track", "forecast_defensible": True},
               "meddpicc": {
                   "champion": {"status": "confirmed", "narrative":
                                "Abe (VP Ops) ran three sessions and owns the pricing decision; advocating internally per the 25 Jun call."},
                   "economic_buyer": {"status": "confirmed", "narrative":
                                      "Chan, the Controller/CFO, controls budget and joined the 20 Jun pricing call — active, not passive."},
                   "identify_pain": {"status": "confirmed", "narrative":
                                     "Manual contract chasing costs ~3 FTE; quantified 12 May."}},
               "competitive_position": {"summary": "Coupa and Zip cut pricing 25 Jun; we lead on fit but under price pressure.",
                                        "competitors": [{"name": "Coupa", "sentiment": "negative", "threat_level": "high", "status": "active"}]},
               "vulnerabilities": {"items": [
                   {"category": "pricing", "detail": "Coupa and Zip undercutting; Abe asked for updated list vs CC pricing.", "status": "open"},
                   {"category": "legal", "detail": "Legal backlog delaying the paper ~2 weeks.", "status": "open"}]}}}


def test_bullets_carry_deal_specific_narrative_and_folded_risk():
    rec = _rich_record()
    rec["ai"]["deal_scores"] = SC.compute_deal_scores(rec)
    panel = CRO.build_cro_panel(rec)
    win = next(b for b in panel["blocks"] if b.get("key") == "win_position")
    joined = " || ".join(b["text"] for b in win["bullets"])
    assert "Abe" in joined or "advocating" in joined      # champion narrative
    assert "Chan" in joined or "Controller" in joined     # EB narrative, not a bare label
    assert any(b["tone"] == "warn" for b in win["bullets"])  # risk folded into win block
    # the stage-cap mechanic must NOT be spoken in the panel (user-directed): no `how` line,
    # and no "caps confidence / anchors near / Shortlisted caps" phrasing anywhere.
    assert not win.get("how")
    import json as _json
    blob = _json.dumps(panel).lower()
    assert "caps confidence" not in blob and "anchors near" not in blob and "cap confidence" not in blob


def test_explicit_negative_survives_keyword_overlay():
    """SARS: a Next-Step keyword must NOT max a factor the sweep scored weak/low."""
    rec = {"hard": {"stage": "Shortlisted"}, "pulse": {"state": "live"},
           "ai": {"champion_strength": {"strength": "weak"},
                  "customer_preference": {"level": "low"},
                  "crm_evidence": {"champion": {"present": True, "src": "Next-Step", "value": "champion"},
                                   "preference": {"present": True, "src": "narrative", "value": "preference for zycus"}},
                  "meddpicc": {"champion": {"status": "weak"}}}}
    s = SC._rubric_win_strengths(rec)
    assert s["champion"] <= 0
    assert s["preference"] <= 0


def test_exec_access_still_lifts_from_named_eb_field():
    rec = {"ai": {"crm_evidence": {"exec_access": {"present": True, "src": "MEDDPICC 2.0", "value": "Jane Doe (CFO)"}},
                  "meddpicc": {"economic_buyer": {"status": "gap"}}}}
    assert SC._rubric_win_strengths(rec)["exec_access"] > 0


def test_scope_shrink_lowers_win_and_surfaces_contribution():
    rec = _rich_record()
    base = SC.compute_deal_scores(rec)["headline"]["win_position"]
    rec["ai"]["scope_change"] = {"direction": "reduced", "detail": "S2P -> S2C; AP module dropped"}
    out = SC.compute_deal_scores(rec)
    assert out["headline"]["win_position"] < base
    assert any(c.get("factor") == "scope_reduced" for c in out["win_position"]["contributions"])


def test_second_panel_floors_exec_access():
    rec = {"ai": {"expansion_context": {"prior_closed_won": True},
                  "meddpicc": {"economic_buyer": {"status": "gap"}}}}
    assert SC._rubric_win_strengths(rec)["exec_access"] >= 0.6


def test_ceo_native_scope_shrink_watch():
    parsed = {"hard": {"amount": 300000},
              "ai": {"deal_scores": {"headline": {"win_position": 55, "deal_momentum": 50}},
                     "scope_change": {"direction": "reduced", "detail": "dropped 2 modules"},
                     "ceo_intervention": {"needed": False}}}
    CEO.finalize_ceo_intervention(parsed, {"amount": 300000}, None)
    ci = parsed["ai"]["ceo_intervention"]
    assert ci["needed"] is True
    assert ci["needs_action"] is False       # a watch, not a CEO action
    assert any(r.get("type") == "scope_shrink" and r.get("severity") == "high" for r in ci["reasons"])
