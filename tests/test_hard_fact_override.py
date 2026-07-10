"""Deterministic tests for the canonical SF hard-fact override (apply_sf_hard_facts).

Proves the SINGLE override path the AI sweep (analyze_one) and the AI-free hard
refresh both use:
  - identity labels are never blanked,
  - governed SF facts are authoritative on a clean read (including clearing a
    model-authored value SF leaves blank),
  - a degraded read only fills, never blanks,
  - days_to_close is server-computed from close_date,
  - manager_name is left to reassert_manager,
  - the four SF date facts are gate-governed and carry a <field>_source.

Run: python3 -m pytest tests/test_hard_fact_override.py -q
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deal_engine_validation as V  # noqa: E402


def _opp(**over):
    base = {
        "id": "0065g00000ABCDEFGHI", "name": "Acme - Source-to-Pay",
        "account": "Acme Corp", "owner_name": "Dana Rep", "owner_id": "0055g0001",
        "manager_name": "Michael McCarthy", "stage": "Negotiation",
        "forecast_category": "Commit", "amount": 250000, "close_date": "2026-09-30",
        "next_step": "Send MSA", "products": "Source-to-Pay", "competitor": "Coupa",
        "ais_score": 80, "ais_status": "AI Hungry", "ais_why": "scaled spend",
        "created_date": "2025-01-15", "last_modified_date": "2026-06-10",
        "last_activity_date": "2026-06-01", "qualified_date": "2025-03-01",
    }
    base.update(over)
    return base


def test_clean_read_overrides_model_facts():
    hard = {"stage": "Qualified", "amount": 999, "competitor": "FAKE",
            "qualified_date": "2099-01-01", "manager_name": "Fake Boss"}
    V.apply_sf_hard_facts(hard, _opp(), authoritative=True)
    assert hard["stage"] == "Negotiation"
    assert hard["amount"] == 250000
    assert hard["competitor"] == "Coupa"
    assert hard["qualified_date"] == "2025-03-01"
    assert hard["last_activity_date"] == "2026-06-01"
    # manager is NOT touched by this helper (reassert_manager owns it)
    assert hard["manager_name"] == "Fake Boss"


def test_clean_read_blanks_sf_empty_fact():
    # SF genuinely has no competitor / status -> a model-authored value is CLEARED.
    hard = {"competitor": "Hallucinated Rival", "ais_status": "AI Hungry"}
    V.apply_sf_hard_facts(hard, _opp(competitor=None, ais_status=None), authoritative=True)
    assert hard["competitor"] is None
    assert hard["ais_status"] is None
    assert hard["ais_score"] == 80  # other facts still applied


def test_degraded_read_never_blanks():
    hard = {"competitor": "Coupa", "stage": "Negotiation"}
    V.apply_sf_hard_facts(hard, {"id": "x"}, authoritative=False)  # degraded/empty snapshot
    assert hard["competitor"] == "Coupa"     # preserved, not blanked
    assert hard["stage"] == "Negotiation"


def test_authoritative_null_stub_never_blanks_the_book():
    # The 2026-07 "$0 everywhere" regression: a failed enrich SOQL returns a null
    # STUB (id + all fields None, NO StageName). Even though the caller passes
    # authoritative=True, a stub must NEVER clear governed facts — else one bad
    # column 400s the query and blanks every deal's amount/stage/forecast/close.
    hard = {"stage": "Negotiation", "amount": 250000, "forecast_category": "Commit",
            "close_date": "2026-09-30", "competitor": "Coupa"}
    stub = {"id": "0065g00000ABCDEFGHI", "name": None, "account": None,
            "owner_name": None, "stage": None, "amount": None,
            "forecast_category": None, "close_date": None, "competitor": None}
    V.apply_sf_hard_facts(hard, stub, authoritative=True)
    assert hard["stage"] == "Negotiation"          # preserved
    assert hard["amount"] == 250000                # preserved
    assert hard["forecast_category"] == "Commit"   # preserved
    assert hard["close_date"] == "2026-09-30"      # preserved
    assert hard["competitor"] == "Coupa"           # preserved


def test_identity_labels_never_blanked_even_on_clean_read():
    hard = {"owner_name": "Dana Rep", "account_name": "Acme Corp", "opp_name": "Acme S2P"}
    V.apply_sf_hard_facts(
        hard, _opp(owner_name=None, account=None, name=None), authoritative=True)
    assert hard["owner_name"] == "Dana Rep"
    assert hard["account_name"] == "Acme Corp"
    assert hard["opp_name"] == "Acme S2P"


def test_days_to_close_computed_from_close_date():
    target = date.today() + timedelta(days=30)
    hard = {"days_to_close": 999}
    V.apply_sf_hard_facts(hard, _opp(close_date=target.isoformat()), authoritative=True)
    assert hard["days_to_close"] == 30


def test_days_to_close_null_when_close_date_missing():
    hard = {"days_to_close": 5}
    V.apply_sf_hard_facts(hard, _opp(close_date=None), authoritative=True)
    assert hard["days_to_close"] is None


def test_new_date_facts_are_governed_and_sourced():
    # The 4 SF dates are now gate-governed and stamped with their SF API source.
    assert {"created_date", "last_modified_date", "last_activity_date",
            "qualified_date"} <= set(V.FACT_SOURCE_FIELDS)
    hard = {}
    V.apply_sf_hard_facts(hard, _opp(), authoritative=True)
    V.stamp_fact_sources(hard, _opp())
    assert hard["qualified_date_source"] == "Qualified_Submission_Date__c"
    assert hard["last_activity_date_source"] == "LastActivityDate"
    assert hard["created_date_source"] == "CreatedDate"


def test_next_step_is_governed_and_sourced():
    # next_step (Next_Step__c) is a deterministic SF fact: overridden, gate-governed
    # and source-stamped so a model-authored next step can't persist unattributed.
    assert "next_step" in V.FACT_SOURCE_FIELDS
    assert V.FACT_SOURCE_FIELDS["next_step"] == "Next_Step__c"
    assert "next_step" in V._SF_KEY
    hard = {"next_step": "model invented this"}
    V.apply_sf_hard_facts(hard, _opp(next_step="Send MSA"), authoritative=True)
    assert hard["next_step"] == "Send MSA"
    V.stamp_fact_sources(hard, _opp(next_step="Send MSA"))
    assert hard["next_step_source"] == "Next_Step__c"
    # SF-null clears the model value AND its source on a clean read.
    hard2 = {"next_step": "stale model step", "next_step_source": "Next_Step__c"}
    V.apply_sf_hard_facts(hard2, _opp(next_step=None), authoritative=True)
    V.stamp_fact_sources(hard2, _opp(next_step=None))
    assert hard2["next_step"] is None
    assert "next_step_source" not in hard2
