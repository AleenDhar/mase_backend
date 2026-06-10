"""Deterministic-core tests for the per-deal living memory (deal_engine_packets).

Covers the three acceptance cases from the task plus the round-trip projection
invariant (a fresh sweep with no prior memory projects back byte-identical item
lists). Run: python3 -m pytest tests/test_deal_engine_packets.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deal_engine_packets as P  # noqa: E402


def _sweep(existing_packets, ai, hard=None, today="2026-06-05"):
    cands = P.extract_candidates(ai, hard or {})
    packets, deltas = P.reconcile(existing_packets, cands, today)
    projected = P.project_into_ai(ai, packets)
    return packets, deltas, projected


def _pkt(packets, key):
    return next((p for p in packets if p.get("key") == key), None)


# ---- Round-trip: first sweep projects back the agent's items unchanged --------

def test_first_sweep_projection_roundtrip():
    ai = {
        "explicit_requirements": {"items": [
            {"requirement": "SAP integration", "said_by": "Jane", "date": "2026-04-01",
             "addressed": False, "quote": "we run SAP"}]},
        "stakeholder_map": {"items": [
            {"name": "Jane Doe", "title": "VP Proc", "role": "Champion",
             "last_contact_date": "2026-04-01", "sentiment": "warm", "risk": ""}]},
        "competitive_position": {"summary": "SAP Ariba in play",
            "competitors": [{"name": "SAP Ariba", "sentiment": "negative",
                             "quote": "too rigid", "date": "2026-04-01"}]},
    }
    packets, deltas, proj = _sweep([], ai)
    assert proj["explicit_requirements"]["items"][0]["requirement"] == "SAP integration"
    assert proj["stakeholder_map"]["items"][0]["name"] == "Jane Doe"
    # summary preserved, competitors projected from packets
    assert proj["competitive_position"]["summary"] == "SAP Ariba in play"
    assert proj["competitive_position"]["competitors"][0]["name"] == "SAP Ariba"
    assert all(d["kind"] == "added" for d in deltas)


# ---- Acceptance 1: scope downgrade -------------------------------------------

def test_scope_downgrade():
    hard1 = {"products": "P2P + ANA"}
    packets1, _, _ = _sweep([], {}, hard1, today="2026-05-01")
    assert _pkt(packets1, "product_scope:scope")["value"]["scope"] == "P2P + ANA"

    hard2 = {"products": "P2P only"}
    packets2, deltas2, proj2 = _sweep(packets1, {}, hard2, today="2026-06-05")
    scope = _pkt(packets2, "product_scope:scope")
    assert scope["value"]["scope"] == "P2P only"
    # old value moved to history
    assert scope["history"][0]["value"]["scope"] == "P2P + ANA"
    # a changed delta is recorded
    changed = [d for d in deltas2 if d["kind"] == "changed" and d["type"] == "product_scope"]
    assert changed and "P2P + ANA" in changed[0]["from"] and "P2P only" in changed[0]["to"]


# ---- Acceptance 2: champion change -------------------------------------------

def test_champion_change():
    ai1 = {"champion_strength": {"champion": "Procurement Manager", "strength": "developing"}}
    packets1, _, _ = _sweep([], ai1, today="2026-05-01")
    assert _pkt(packets1, "champion:champion")["value"]["name"] == "Procurement Manager"

    ai2 = {"champion_strength": {"champion": "Director Procurement", "strength": "strong"}}
    packets2, deltas2, _ = _sweep(packets1, ai2, today="2026-06-05")
    champ = _pkt(packets2, "champion:champion")
    assert champ["value"]["name"] == "Director Procurement"
    assert champ["history"][0]["value"]["name"] == "Procurement Manager"
    changed = [d for d in deltas2 if d["kind"] == "changed" and d["type"] == "champion"]
    assert changed and "Procurement Manager" in changed[0]["from"]
    assert "Director Procurement" in changed[0]["to"]


# ---- Acceptance 3: retain-on-absence (durable) -------------------------------

def test_retain_on_absence():
    ai1 = {"explicit_requirements": {"items": [
        {"requirement": "SAP integration", "said_by": "Jane", "date": "2026-04-01",
         "addressed": False}]}}
    packets1, _, _ = _sweep([], ai1, today="2026-05-01")

    # Sweep 2 mentions a different requirement; SAP integration is NOT repeated.
    ai2 = {"explicit_requirements": {"items": [
        {"requirement": "SSO", "said_by": "Bob", "date": "2026-06-01",
         "addressed": False}]}}
    packets2, deltas2, proj2 = _sweep(packets1, ai2, today="2026-06-05")

    sap = _pkt(packets2, "requirement:sap-integration")
    assert sap is not None, "durable requirement must be retained"
    assert sap["status"] == "dormant"
    assert sap["last_confirmed"] == "2026-05-01"  # unchanged
    # still appears in the projected dashboard requirements
    reqs = [r["requirement"] for r in proj2["explicit_requirements"]["items"]]
    assert "SAP integration" in reqs and "SSO" in reqs
    assert any(d["kind"] == "dormant" and "sap" in d["key"] for d in deltas2)


# ---- Safety: re-confirm reactivates a dormant fact ---------------------------

def test_reactivation():
    ai1 = {"explicit_requirements": {"items": [{"requirement": "SAP integration"}]}}
    p1, _, _ = _sweep([], ai1, today="2026-05-01")
    p2, _, _ = _sweep(p1, {}, today="2026-05-15")  # absent -> dormant
    assert _pkt(p2, "requirement:sap-integration")["status"] == "dormant"
    p3, d3, _ = _sweep(p2, ai1, today="2026-06-05")  # re-mentioned
    assert _pkt(p3, "requirement:sap-integration")["status"] == "active"
    assert any(d["kind"] == "reactivated" for d in d3)


# ---- Safety: addressed flag flip is a change, requirement stays one item -----

def test_no_noise_delta_on_volatile_field_only():
    # Same requirement, same addressed state, but a refreshed date/quote each
    # sweep must NOT spam a `changed` delta (it just freshens the value).
    ai1 = {"explicit_requirements": {"items": [
        {"requirement": "SAP integration", "addressed": False,
         "date": "2026-05-01", "quote": "we run SAP"}]}}
    p1, _, _ = _sweep([], ai1, today="2026-05-01")
    ai2 = {"explicit_requirements": {"items": [
        {"requirement": "SAP integration", "addressed": False,
         "date": "2026-06-05", "quote": "still on SAP, same need"}]}}
    p2, d2, proj2 = _sweep(p1, ai2, today="2026-06-05")
    assert not any(d["kind"] == "changed" for d in d2), "volatile-only change must not log a delta"
    # value still refreshed to the latest evidence
    sap = _pkt(p2, "requirement:sap-integration")
    assert sap["value"]["date"] == "2026-06-05"
    assert sap["last_confirmed"] == "2026-06-05"


def test_champion_backfilled_when_omitted():
    # Sweep 1 has a champion; sweep 2 omits champion_strength entirely. The
    # dashboard's champion field must be backfilled from the retained packet.
    ai1 = {"champion_strength": {"champion": "Jane Doe", "strength": "strong"}}
    p1, _, _ = _sweep([], ai1, today="2026-05-01")
    p2, _, proj2 = _sweep(p1, {}, today="2026-06-05")  # champion omitted
    assert proj2["champion_strength"]["champion"] == "Jane Doe"
    assert proj2["champion_strength"]["strength"] == "strong"


def test_scope_retained_when_products_omitted():
    # product_scope is non-durable (state): if a sweep omits hard.products, the
    # last known scope is retained and still projected.
    p1, _, _ = _sweep([], {}, {"products": "P2P + ANA"}, today="2026-05-01")
    p2, d2, proj2 = _sweep(p1, {}, {}, today="2026-06-05")  # no products
    assert _pkt(p2, "product_scope:scope")["status"] == "active"  # not dormant
    assert proj2["product_scope"]["scope"] == "P2P + ANA"


def test_duplicate_same_key_candidates_no_self_churn():
    # Two candidates with the same key in one sweep must not generate a spurious
    # change delta against each other.
    ai = {"explicit_requirements": {"items": [
        {"requirement": "SAP integration", "addressed": False, "date": "2026-06-01"},
        {"requirement": "SAP integration", "addressed": False, "date": "2026-06-05"}]}}
    packets, deltas, proj = _sweep([], ai, today="2026-06-05")
    reqs = [p for p in packets if p["key"] == "requirement:sap-integration"]
    assert len(reqs) == 1
    assert sum(1 for d in deltas if d["kind"] == "added") == 1
    assert not any(d["kind"] == "changed" for d in deltas)


def test_requirement_addressed_flip():
    ai1 = {"explicit_requirements": {"items": [
        {"requirement": "SAP integration", "addressed": False}]}}
    p1, _, _ = _sweep([], ai1, today="2026-05-01")
    ai2 = {"explicit_requirements": {"items": [
        {"requirement": "SAP integration", "addressed": True}]}}
    p2, d2, proj2 = _sweep(p1, ai2, today="2026-06-05")
    items = proj2["explicit_requirements"]["items"]
    assert len(items) == 1 and items[0]["addressed"] is True
    assert any(d["kind"] == "changed" for d in d2)


# ---- "What changed" panel: human label + rep-facing group --------------------

def test_delta_group_buckets():
    assert P.delta_group("added") == "added"
    assert P.delta_group("reactivated") == "added"
    assert P.delta_group("changed") == "changed"
    assert P.delta_group("resolved") == "resolved"
    assert P.delta_group("superseded") == "resolved"
    assert P.delta_group("dormant") == "dormant"
    # unknown / empty kinds fall back to "changed" without raising
    assert P.delta_group("whatever") == "changed"
    assert P.delta_group(None) == "changed"


def test_delta_label_phrasing():
    assert P.delta_label({"kind": "added", "type": "requirement",
                          "subject": "SAP integration"}) == "New requirement: SAP integration"
    assert P.delta_label({"kind": "resolved", "type": "risk",
                          "subject": "budget freeze"}) == "Risk resolved: budget freeze"
    assert P.delta_label({"kind": "dormant", "type": "requirement",
                          "subject": "SSO"}) == "Requirement went quiet: SSO"
    assert P.delta_label({"kind": "reactivated", "type": "competitor",
                          "subject": "SAP Ariba"}) == "Competitor back in play: SAP Ariba"
    # generic subjects (champion/scope) are dropped from the headline
    assert P.delta_label({"kind": "changed", "type": "champion",
                          "subject": "champion"}) == "Champion updated"
    assert P.delta_label({"kind": "changed", "type": "product_scope",
                          "subject": "scope"}) == "Product scope updated"
    # never raises on a malformed/empty delta
    assert isinstance(P.delta_label({}), str)
    assert P.delta_label(None) == ""


def test_present_delta_is_nondestructive():
    raw = {"kind": "added", "type": "stakeholder", "subject": "Jane Doe",
           "date": "2026-06-05", "key": "stakeholder:jane-doe"}
    out = P.present_delta(raw)
    # original fields preserved + label/group added
    assert out["subject"] == "Jane Doe" and out["date"] == "2026-06-05"
    assert out["label"] == "New stakeholder: Jane Doe"
    assert out["group"] == "added"
    # source dict untouched
    assert "label" not in raw


# ---- Seeding: one-time baseline for un-re-swept deals -----------------------

def test_seed_packets_builds_baseline_without_deltas():
    # A pre-living-memory record: facts live only in ai.*/hard. Seeding must
    # produce the same packet store a fresh reconcile would, but emit NO deltas
    # (pre-existing facts are not "added" changes).
    ai = {
        "explicit_requirements": {"items": [
            {"requirement": "SAP integration", "said_by": "Jane",
             "addressed": False}]},
        "stakeholder_map": {"items": [
            {"name": "Jane Doe", "role": "Champion"}]},
        "champion_strength": {"champion": "Jane Doe", "strength": "strong"},
    }
    hard = {"products": "P2P + ANA"}
    seeded = P.seed_packets(ai, hard, "2026-05-01")

    # Same packet keys a normal first sweep would mint.
    reconciled, deltas = P.reconcile([], P.extract_candidates(ai, hard), "2026-05-01")
    assert {p["key"] for p in seeded} == {p["key"] for p in reconciled}
    # The reconcile path would have logged `added` deltas; seeding logs none.
    assert any(d["kind"] == "added" for d in deltas)

    # first_seen is pinned to the as_of (baseline) date and packets are active.
    for p in seeded:
        assert p["first_seen"] == "2026-05-01"
        assert p["last_confirmed"] == "2026-05-01"
        assert p["status"] == "active"


def test_seed_then_real_sweep_logs_only_genuine_changes():
    # After seeding a baseline, the NEXT real sweep must log only genuine changes
    # (a flipped requirement), never re-announce the seeded facts as new.
    ai1 = {"explicit_requirements": {"items": [
        {"requirement": "SAP integration", "addressed": False}]},
        "champion_strength": {"champion": "Jane Doe", "strength": "strong"}}
    baseline = P.seed_packets(ai1, {}, "2026-05-01")

    ai2 = {"explicit_requirements": {"items": [
        {"requirement": "SAP integration", "addressed": True}]},
        "champion_strength": {"champion": "Jane Doe", "strength": "strong"}}
    _, deltas = P.reconcile(baseline, P.extract_candidates(ai2, {}), "2026-06-05")
    kinds = {d["kind"] for d in deltas}
    assert "added" not in kinds, "seeded facts must not re-announce as added"
    assert any(d["kind"] == "changed" and d["type"] == "requirement"
               for d in deltas)


def test_seed_empty_record_yields_no_packets():
    # A record with no extractable facts seeds to an empty store and no deltas.
    assert P.seed_packets({}, {}, "2026-05-01") == []


def test_real_sweep_deltas_all_labelled():
    # Every delta a real reconcile emits must produce a non-empty label and a
    # valid group bucket (guards against an unhandled kind/type).
    ai1 = {"explicit_requirements": {"items": [{"requirement": "SAP integration"}]},
           "champion_strength": {"champion": "Jane", "strength": "weak"}}
    p1, d1, _ = _sweep([], ai1, today="2026-05-01")
    ai2 = {"champion_strength": {"champion": "Bob", "strength": "strong"}}
    _, d2, _ = _sweep(p1, ai2, today="2026-06-05")  # champion changed + SAP dormant
    for d in d1 + d2:
        pd = P.present_delta(d)
        assert pd["label"], f"empty label for {d}"
        assert pd["group"] in ("added", "changed", "resolved", "dormant")
