"""Deterministic tests for the sweep anti-fabrication gate (deal_engine_validation).

These prove the structural guarantees of Task #89 without an agent / network:
  * manager_name is server-owned (the model never wins).
  * a fabricated, unsourced, unknown person cannot persist...
  * ...but a real SF contact, an Avoma-discovered person WITH a source, and a
    name already on the prior record all survive.
  * template/placeholder leakage is scrubbed.

Run: python3 -m pytest tests/test_sweep_validation_gate.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deal_engine_validation as V  # noqa: E402
import deal_engine_packets as P  # noqa: E402


# ---- manager is server-owned -------------------------------------------------

def test_manager_overrides_model_value():
    hard = {"manager_name": "Totally Fake Boss"}
    opp = {"manager_name": "Real Manager"}
    changed = V.reassert_manager(hard, opp)
    assert hard["manager_name"] == "Real Manager"
    assert changed is True


def test_manager_is_none_when_salesforce_has_none():
    # No ground truth -> the field is honestly null, never the model's guess.
    hard = {"manager_name": "Invented Person"}
    V.reassert_manager(hard, {"manager_name": None})
    assert hard["manager_name"] is None
    hard2 = {"manager_name": "Invented Person"}
    V.reassert_manager(hard2, {})  # manager key absent entirely
    assert hard2["manager_name"] is None


def test_resolve_manager_name():
    assert V.resolve_manager_name({"manager_name": "A Boss"}) == "A Boss"
    assert V.resolve_manager_name({"manager_name": ""}) is None
    assert V.resolve_manager_name({}) is None


def test_manager_fabricated_only_flags_a_real_wrong_name():
    # A concrete wrong name that contradicts ground truth is a fabrication.
    assert V.manager_fabricated({"manager_name": "Totally Fake Boss"},
                                {"manager_name": "Real Manager"}) is True
    # Omitting it (the new normal — server fills it in) is NOT a fabrication,
    # even when Salesforce has a manager.
    assert V.manager_fabricated({}, {"manager_name": "Real Manager"}) is False
    assert V.manager_fabricated({"manager_name": None},
                                {"manager_name": "Real Manager"}) is False
    assert V.manager_fabricated({"manager_name": ""},
                                {"manager_name": "Real Manager"}) is False
    # A model value that matches ground truth (modulo whitespace/case) is fine.
    assert V.manager_fabricated({"manager_name": "real  manager"},
                                {"manager_name": "Real Manager"}) is False


# ---- allowlist ---------------------------------------------------------------

def _buyer():
    return {"contacts": [{"name": "Jane Buyer", "title": "VP", "email": "j@acme.com"}],
            "task_contacts": ["Tom Task"]}


def test_allowlist_grandfathers_only_sourced_prior_names():
    # Live SF contacts/tasks are always vouched for. Prior-record names are
    # grandfathered ONLY when they carried provenance (a source / quote) — an
    # unsourced prior name may be a pre-gate fabrication and is NOT trusted.
    prior = {
        "ai": {
            "stakeholder_map": {"items": [
                {"name": "Sourced Stakeholder", "source": "Avoma 2 Feb"},
                {"name": "Unsourced Stakeholder"}]},
            "champion_strength": {"champion": "Sourced Champ", "source": "Avoma 3 Mar"},
            "explicit_requirements": {"items": [
                {"said_by": "Quoted Asker", "quote": "send SOC2"}]},
        },
        "packets": [{"value": {"name": "Dormant Sourced", "source": "Avoma 4 Apr"}},
                    {"value": {"name": "Dormant Unsourced"}}],
    }
    allow = V.build_people_allowlist(_buyer(), prior)
    for n in ("jane buyer", "tom task", "sourced stakeholder", "sourced champ",
              "quoted asker", "dormant sourced"):
        assert n in allow
    for n in ("unsourced stakeholder", "dormant unsourced"):
        assert n not in allow


# ---- stakeholders ------------------------------------------------------------

def test_fabricated_stakeholder_dropped_real_and_sourced_kept():
    ai = {"stakeholder_map": {"items": [
        {"name": "Jane Buyer", "role": "Champion", "source": ""},        # allowlisted
        {"name": "Sourced Sam", "role": "Influencer",
         "source": "Avoma discovery call 12 May"},                        # Avoma + source
        {"name": "Ghost McFake", "role": "Economic Buyer", "source": ""}, # fabricated
        {"name": "", "role": "Unknown"},                                  # empty
    ]}}
    allow = V.build_people_allowlist(_buyer(), {})
    violations = V.sanitize_people(ai, allow)
    names = [s["name"] for s in ai["stakeholder_map"]["items"]]
    assert names == ["Jane Buyer", "Sourced Sam"]
    assert any("Ghost McFake" in v for v in violations)
    assert any("no name" in v for v in violations)


# ---- champion ----------------------------------------------------------------

def test_fabricated_champion_cleared():
    ai = {"champion_strength": {"champion": "Ghost Champ", "strength": "strong",
                                "source": ""}}
    V.sanitize_people(ai, V.build_people_allowlist(_buyer(), {}))
    assert ai["champion_strength"]["champion"] == ""
    assert ai["champion_strength"]["strength"] == "none"


def test_real_champion_kept():
    ai = {"champion_strength": {"champion": "Jane Buyer", "strength": "strong"}}
    V.sanitize_people(ai, V.build_people_allowlist(_buyer(), {}))
    assert ai["champion_strength"]["champion"] == "Jane Buyer"


def test_sourced_champion_kept():
    ai = {"champion_strength": {"champion": "Avoma Only", "strength": "developing",
                                "source": "Avoma demo 3 Jun"}}
    V.sanitize_people(ai, V.build_people_allowlist(_buyer(), {}))
    assert ai["champion_strength"]["champion"] == "Avoma Only"


# ---- said_by -----------------------------------------------------------------

def test_unverifiable_said_by_blanked_but_requirement_kept():
    ai = {"explicit_requirements": {"items": [
        {"requirement": "SOC2 report", "said_by": "Ghost Asker", "quote": ""}]}}
    V.sanitize_people(ai, V.build_people_allowlist(_buyer(), {}))
    item = ai["explicit_requirements"]["items"][0]
    assert item["said_by"] == ""
    assert item["requirement"] == "SOC2 report"  # the ask itself survives


def test_said_by_kept_when_item_quoted():
    ai = {"explicit_requirements": {"items": [
        {"requirement": "SOC2 report", "said_by": "Avoma Speaker",
         "quote": "send me your SOC2"}]}}
    V.sanitize_people(ai, V.build_people_allowlist(_buyer(), {}))
    assert ai["explicit_requirements"]["items"][0]["said_by"] == "Avoma Speaker"


# ---- grandfathering prior names ---------------------------------------------

def test_prior_record_sourced_name_survives_on_resweep():
    # A name on the prior record that carried a source is grandfathered, so a
    # re-emitted (now sourceless) copy is NOT re-flagged as fabricated.
    prior = {"ai": {"stakeholder_map": {"items": [
        {"name": "Old Stakeholder", "source": "Avoma 1 Jan"}]}}}
    ai = {"stakeholder_map": {"items": [
        {"name": "Old Stakeholder", "role": "Coach", "source": ""}]}}
    V.sanitize_people(ai, V.build_people_allowlist(_buyer(), prior))
    assert [s["name"] for s in ai["stakeholder_map"]["items"]] == ["Old Stakeholder"]


def test_resweep_drops_legacy_fabricated_name_keeps_sourced():
    # The prior record holds a pre-gate fabrication (no source) AND a legitimately
    # sourced person. On a hardened re-sweep the agent re-emits BOTH sourceless;
    # only the unsourced legacy fabrication is cleaned out.
    prior = {"ai": {"stakeholder_map": {"items": [
        {"name": "Legacy Ghost"},                              # no source
        {"name": "Sourced Veteran", "source": "Avoma 1 Mar"}]}}}  # sourced
    ai = {"stakeholder_map": {"items": [
        {"name": "Legacy Ghost", "role": "Coach", "source": ""},
        {"name": "Sourced Veteran", "role": "Influencer", "source": ""}]}}
    V.sanitize_people(ai, V.build_people_allowlist(_buyer(), prior))
    names = [s["name"] for s in ai["stakeholder_map"]["items"]]
    assert "Legacy Ghost" not in names      # legacy fabrication cleaned on re-sweep
    assert names == ["Sourced Veteran"]     # the sourced, legit name survives


# ---- placeholder scrub -------------------------------------------------------

def test_scrub_removes_template_leakage():
    parsed = {"hard": {"manager_name": "manager_name",
                       "sf_link": "https://x/lightning/r/Opportunity/<id>/view"},
              "ai": {"north_star_verdict": {
                  "headline": "historical record from prior sweep"}},
              "packets": [{"value": {"name": "keep <this> packet"}}]}
    n = V.scrub_record(parsed)
    assert n >= 3
    assert parsed["hard"]["manager_name"] == ""
    assert "<id>" not in parsed["hard"]["sf_link"]
    assert parsed["ai"]["north_star_verdict"]["headline"] == ""
    # packets are server-managed and left untouched by scrub_record
    assert parsed["packets"][0]["value"]["name"] == "keep <this> packet"


def test_scrub_leaves_clean_text_alone():
    parsed = {"ai": {"x": "Met with the VP of Procurement on 3 June; strong intent."},
              "hard": {"stage": "Negotiation"}}
    assert V.scrub_record(parsed) == 0
    assert parsed["ai"]["x"].startswith("Met with the VP")


def test_scrub_removes_square_bracket_placeholders():
    # The [CFO name] gap: square-bracket template slots must be neutralised, while
    # numeric citations and markdown links are left intact.
    parsed = {"ai": {"recommended_moves": {"items": [
        {"action": "Escalate to the [CFO name] before the [European public sector "
                   "customer] signs."},
        {"action": "Confirm budget with [X]."}]}},
        "hard": {"stage": "Negotiation"}}
    n = V.scrub_record(parsed)
    assert n >= 2
    a0 = parsed["ai"]["recommended_moves"]["items"][0]["action"]
    a1 = parsed["ai"]["recommended_moves"]["items"][1]["action"]
    assert "[" not in a0 and "]" not in a0
    assert a0 == "Escalate to the before the signs."
    assert a1 == "Confirm budget with."


def test_scrub_leaves_citations_and_links_alone():
    parsed = {"ai": {"x": "Per the QBR [1], renewal is on track; see [docs](http://x)."},
              "hard": {"stage": "Negotiation"}}
    assert V.scrub_record(parsed) == 0
    assert parsed["ai"]["x"].startswith("Per the QBR [1]")


def test_scrub_placeholders_recurses_lists_and_dicts():
    obj = {"a": ["plain", "<token>", {"b": "manager_name"}]}
    cleaned, n = V.scrub_placeholders(obj)
    assert n == 2
    assert cleaned["a"][0] == "plain"
    assert cleaned["a"][1] == ""
    assert cleaned["a"][2]["b"] == ""


# ---- Part 4: the validate_record gate REJECTS fabrication --------------------

# Authoritative Salesforce snapshot the gate validates against (the `_map_opps`
# shape analyze_one passes as `sf_facts`).
def _sf():
    return {"manager_name": "Michael McCarthy", "owner_name": "Dana Rep",
            "account": "Acme Corp", "stage": "Negotiation", "amount": 250000,
            "close_date": "2026-09-30", "forecast_category": "Commit",
            "competitor": "Coupa", "products": "Source-to-Pay",
            "ais_score": 80, "ais_status": "AI Hungry", "ais_why": "scaled spend"}


def _good_record():
    """A record whose every governed fact matches SF and carries a source, and
    whose only people are a real contact / a sourced Avoma speaker."""
    hard = {"manager_name": "Michael McCarthy", "owner_name": "Dana Rep",
            "account_name": "Acme Corp", "stage": "Negotiation", "amount": 250000,
            "close_date": "2026-09-30", "forecast_category": "Commit",
            "competitor": "Coupa", "products": "Source-to-Pay",
            "ais_score": 80, "ais_status": "AI Hungry", "ais_why": "scaled spend"}
    V.stamp_fact_sources(hard, _sf())
    return {
        "hard": hard,
        "ai": {
            "stakeholder_map": {"items": [
                {"name": "Jane Buyer", "role": "Champion", "source": ""},
                {"name": "Sourced Sam", "role": "Influencer",
                 "source": "Avoma discovery 12 May"}]},
            "champion_strength": {"champion": "Jane Buyer", "strength": "strong"},
            "recommended_moves": {"items": [
                {"rank": 1, "action": "Book an executive connect with the deal "
                 "owner's manager to sponsor the close."}]},
            "best_practice_check": {"flags": ["Single-threaded — widen access."]},
        },
        "evidence_coverage": {"gaps": [], "calls_read": 3},
    }


def _contacts():
    return [{"name": "Jane Buyer"}]


def test_validate_record_passes_a_clean_record():
    assert V.validate_record(_good_record(), sf_facts=_sf(),
                             contact_roles=_contacts(),
                             avoma_attendees=["Sourced Sam"],
                             active_sf_user_names={"Dana Rep"}) == []


def test_validate_record_REJECTS_fabricated_manager_and_placeholder():
    """The spec's required regression: a record naming a non-existent manager and
    leaking a placeholder token is REJECTED (gate returns violations)."""
    rec = _good_record()
    # 1) fabricated manager in the hard block
    rec["hard"]["manager_name"] = "Mark Emery"          # SF says Michael McCarthy
    rec["hard"]["manager_name_source"] = "Owner.Manager.Name"
    # 2) a manager-slot name in a to-do + a literal placeholder token
    rec["ai"]["recommended_moves"]["items"][0]["action"] = (
        "Executive connect via Mark Emery (manager); see manager_name field.")
    violations = V.validate_record(rec, sf_facts=_sf(), contact_roles=_contacts(),
                                   avoma_attendees=["Sourced Sam"],
                                   active_sf_user_names={"Dana Rep"})
    assert violations, "gate must REJECT a fabricated manager + placeholder"
    checks = {v["check"] for v in violations}
    assert "manager" in checks
    assert "placeholder" in checks


def test_validate_record_rejects_unverifiable_person():
    rec = _good_record()
    rec["ai"]["stakeholder_map"]["items"].append(
        {"name": "Ghost McFake", "role": "Economic Buyer", "source": ""})
    violations = V.validate_record(rec, sf_facts=_sf(), contact_roles=_contacts(),
                                   avoma_attendees=["Sourced Sam"],
                                   active_sf_user_names={"Dana Rep"})
    assert any(v["check"] == "person" and "Ghost" in str(v["offending"])
               for v in violations)


def test_validate_record_rejects_invented_person_in_move_text():
    # Part 4 / finding #1+#2: a fabricated person named in ANY to-do/action text
    # (not just a manager slot) must be REJECTED, even outside recommended_moves.
    rec = _good_record()
    rec["ai"]["recommended_moves"]["items"][0]["action"] = (
        "Escalate to Ghost Stranger to unblock procurement before close.")
    rec["ai"]["best_practice_check"]["flags"] = [
        "Loop in Phantom Exec (sponsor) to re-engage."]
    violations = V.validate_record(rec, sf_facts=_sf(), contact_roles=_contacts(),
                                   avoma_attendees=["Sourced Sam"],
                                   active_sf_user_names={"Dana Rep"})
    offenders = " ".join(str(v["offending"]) for v in violations
                         if v["check"] == "person")
    assert "Ghost Stranger" in offenders
    assert "Phantom Exec" in offenders


def test_validate_record_keeps_known_person_in_move_text():
    # A KNOWN person (an SF contact) named in a to-do item is fine — no false
    # positive — and a role phrase after a cue ("align with Decision Makers") is
    # never mistaken for a person.
    rec = _good_record()
    rec["ai"]["recommended_moves"]["items"][0]["action"] = (
        "Loop in Jane Buyer to confirm the SOW, then align with Decision Makers.")
    assert V.validate_record(rec, sf_facts=_sf(), contact_roles=_contacts(),
                             avoma_attendees=["Sourced Sam"],
                             active_sf_user_names={"Dana Rep"}) == []


def test_validate_record_rejects_hard_fact_divergence_and_missing_source():
    rec = _good_record()
    rec["hard"]["amount"] = 999999          # SF says 250000
    rec["hard"].pop("close_date_source")    # value present, source removed
    violations = V.validate_record(rec, sf_facts=_sf(), contact_roles=_contacts(),
                                   avoma_attendees=["Sourced Sam"],
                                   active_sf_user_names={"Dana Rep"})
    checks = {v["check"] for v in violations}
    assert "hard_fact" in checks
    assert "source" in checks


def test_sanitize_failed_record_makes_a_failing_record_pass():
    """After retries are exhausted, the last-resort sanitizer produces a record
    that the gate accepts — proving the pipeline can ALWAYS persist a safe record."""
    rec = _good_record()
    rec["hard"]["manager_name"] = "Mark Emery"
    rec["hard"]["amount"] = 999999
    rec["ai"]["recommended_moves"]["items"][0]["action"] = (
        "Executive connect via Mark Emery (manager); see manager_name field.")
    rec["ai"]["stakeholder_map"]["items"].append(
        {"name": "Ghost McFake", "role": "Economic Buyer", "source": ""})
    allow = V.build_people_allowlist({"contacts": _contacts(), "task_contacts": []}, {})
    allow |= {V._norm_name("Sourced Sam")}
    violations = V.validate_record(rec, sf_facts=_sf(), contact_roles=_contacts(),
                                   avoma_attendees=["Sourced Sam"],
                                   active_sf_user_names={"Dana Rep"})
    assert violations
    V.sanitize_failed_record(rec, violations, _sf(), allowlist=allow)
    # the sanitized record now PASSES the gate
    assert V.validate_record(rec, sf_facts=_sf(), contact_roles=_contacts(),
                             avoma_attendees=["Sourced Sam"],
                             active_sf_user_names={"Dana Rep"}) == []
    assert rec["hard"]["manager_name"] == "Michael McCarthy"
    assert rec["hard"]["amount"] == 250000
    assert "Mark Emery" not in rec["ai"]["recommended_moves"]["items"][0]["action"]
    assert all(s["name"] != "Ghost McFake"
               for s in rec["ai"]["stakeholder_map"]["items"])
    assert any("validation gate" in g for g in rec["evidence_coverage"]["gaps"])


# ---- Part 4: the PACKET STORE never inherits a fabrication -------------------
# Living memory builds the durable packets from ai.* ONLY AFTER the gate has
# sanitised it, so a fabrication the gate strips from ai can never survive in the
# packet store (the source of truth) and re-project on a later sweep.

def _packet_text(packets):
    """Flatten every string a packet carries (subject + nested value + source)."""
    out = []

    def _walk(v):
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                _walk(x)
        elif isinstance(v, list):
            for x in v:
                _walk(x)

    for p in packets:
        out.append(str(p.get("subject") or ""))
        out.append(str(p.get("source") or ""))
        _walk(p.get("value"))
    return " || ".join(out)


def test_packets_built_from_sanitized_ai_carry_no_fabrication():
    """Regression for the packet-persistence bypass: after the gate exhausts its
    retries and deterministically sanitises a record, the packets extracted from
    the resulting ai (what _apply_living_memory persists) contain NO invented
    person and NO placeholder token — for every packet-backed section
    (requirements, deliverables, vulnerabilities, best-practice flags,
    stakeholders)."""
    rec = _good_record()
    # Fabricated people named in packet-backed action TEXT, a fabricated structured
    # stakeholder, and a placeholder leak inside a packet-backed flag.
    rec["ai"]["explicit_requirements"] = {"items": [
        {"requirement": "Send the SOC2 report to Ghost Asker before close.",
         "said_by": "Ghost Asker", "quote": ""}]}
    rec["ai"]["open_deliverables"] = {"items": [
        {"commitment": "Loop in Phantom Exec to deliver revised pricing.",
         "who": "them"}]}
    rec["ai"]["vulnerabilities"] = {"items": [
        {"detail": "Escalate to Spectre Villain to unblock procurement.",
         "category": "stakeholder"}]}
    rec["ai"]["best_practice_check"] = {"flags": [
        "Loop in Invisible Sponsor (sponsor) to re-engage.",
        "Refresh the deal per <id> guidance.",
        "Single-threaded — widen access."]}
    rec["ai"]["stakeholder_map"]["items"].append(
        {"name": "Ghost McFake", "role": "Economic Buyer", "source": ""})

    allow = V.build_people_allowlist({"contacts": _contacts(), "task_contacts": []}, {})
    allow |= {V._norm_name("Sourced Sam")}

    # Mirror the real pipeline order: _finalize people-gate + placeholder scrub,
    # then the gate's last-resort free-text sanitiser, THEN packet extraction.
    V.sanitize_people(rec["ai"], allow)
    V.scrub_record(rec)
    violations = V.validate_record(rec, sf_facts=_sf(), contact_roles=_contacts(),
                                   avoma_attendees=["Sourced Sam"],
                                   active_sf_user_names={"Dana Rep"})
    assert violations
    V.sanitize_failed_record(rec, violations, _sf(), allowlist=allow)

    packets = P.extract_candidates(rec["ai"], rec["hard"])
    blob = _packet_text(packets)
    for ghost in ("Ghost Asker", "Phantom Exec", "Spectre Villain",
                  "Invisible Sponsor", "Ghost McFake"):
        assert ghost not in blob, f"{ghost!r} leaked into the packet store: {blob}"
    assert "<id>" not in blob

    # Belt and suspenders: the sanitised record itself now passes the gate, so the
    # projection that re-derives ai from these packets stays clean too.
    assert V.validate_record(rec, sf_facts=_sf(), contact_roles=_contacts(),
                             avoma_attendees=["Sourced Sam"],
                             active_sf_user_names={"Dana Rep"}) == []


# ---- packet-level gate (carried-forward poison) ------------------------------
# Finding-2 regression: the per-attempt gate cleans only THIS sweep's raw output,
# but living memory MERGES in carried-forward packets. A packet minted by a
# pre-gate sweep can hold a fabricated person / placeholder; sanitize_packets must
# remove it BEFORE project_into_ai re-introduces it into ai.* post-validation.

def _pkt(ptype, subject, value, source="ai:section", status="active"):
    return {"type": ptype, "subject": subject, "value": value, "source": source,
            "status": status, "first_seen": "2026-01-01",
            "last_confirmed": "2026-01-01", "last_updated": "2026-01-01",
            "key": P.make_key(ptype, subject)}


def test_sanitize_packets_drops_unsourced_unknown_stakeholder():
    allow = {V._norm_name("Real Contact")}
    pkts = [
        _pkt("stakeholder", "Ghost McFake",
             {"name": "Ghost McFake", "role": "Economic Buyer", "source": ""}),
        _pkt("stakeholder", "Real Contact",
             {"name": "Real Contact", "role": "Champion", "source": ""}),
        _pkt("stakeholder", "Sourced Sam",
             {"name": "Sourced Sam", "role": "Coach", "source": "Avoma 4 Apr"}),
    ]
    clean, n = V.sanitize_packets(pkts, allow, {})
    names = {p["subject"] for p in clean}
    assert "Ghost McFake" not in names
    assert {"Real Contact", "Sourced Sam"} <= names
    assert n == 1


def test_sanitize_packets_drops_unverifiable_champion_keeps_sourced():
    pkts = [
        _pkt("champion", "champion", {"name": "Fake Champ", "strength": "strong"}),
        _pkt("champion", "champion",
             {"name": "Sourced Champ", "strength": "strong", "source": "Avoma"}),
    ]
    clean, n = V.sanitize_packets(pkts, set(), {})
    kept = [p["value"]["name"] for p in clean]
    assert "Fake Champ" not in kept
    assert "Sourced Champ" in kept
    assert n == 1


def test_sanitize_packets_drops_placeholder_packet():
    pkts = [
        _pkt("hygiene", "Refresh the deal per <id> guidance.",
             {"flag": "Refresh the deal per <id> guidance."}),
        _pkt("hygiene", "Single-threaded — widen access.",
             {"flag": "Single-threaded — widen access."}),
    ]
    clean, n = V.sanitize_packets(pkts, set(), {})
    subs = {p["subject"] for p in clean}
    assert "Single-threaded — widen access." in subs
    assert len(clean) == 1 and n == 1


def test_sanitize_packets_drops_freetext_person_in_subject():
    pkts = [
        _pkt("requirement", "Send the SOC2 report to Ghost Asker before close.",
             {"requirement": "Send the SOC2 report to Ghost Asker before close.",
              "kind": "explicit"})]
    clean, n = V.sanitize_packets(pkts, set(), _sf())
    assert clean == [] and n == 1


def test_sanitize_packets_blanks_unverifiable_said_by_keeps_requirement():
    pkts = [
        _pkt("requirement", "Provide a SOC2 report",
             {"requirement": "Provide a SOC2 report", "said_by": "Ghost Asker",
              "kind": "explicit"})]
    clean, n = V.sanitize_packets(pkts, set(), _sf())
    assert len(clean) == 1
    assert clean[0]["value"]["said_by"] == ""
    assert n == 1


def test_poisoned_existing_packets_dropped_before_projection():
    """End-to-end finding-2 path: a poisoned packet already in the store is merged
    by reconcile, but sanitize_packets removes it BEFORE project_into_ai, so the
    re-derived ai carries no fabrication."""
    poisoned_prior = [
        _pkt("stakeholder", "Ghost McFake",
             {"name": "Ghost McFake", "role": "Economic Buyer", "source": ""}),
        _pkt("hygiene", "Refresh the deal per <id> guidance.",
             {"flag": "Refresh the deal per <id> guidance."}),
        _pkt("stakeholder", "Real Contact",
             {"name": "Real Contact", "role": "Champion", "source": "Avoma 1 May"}),
    ]
    # This sweep emits no new candidates -> merged store == carried-forward store.
    merged, _ = P.reconcile(poisoned_prior, [], "2026-06-15")
    allow = V.build_people_allowlist(
        {"contacts": _contacts(), "task_contacts": []}, {})
    clean, n = V.sanitize_packets(merged, allow, _sf())
    assert n >= 2
    ai = P.project_into_ai({}, clean)
    blob = _packet_text(clean) + " || " + str(ai)
    assert "Ghost McFake" not in blob
    assert "<id>" not in blob
    assert "Real Contact" in blob


# ---- finding-3 gate: ai.meddpicc free-text narratives are NOT covered by --------
# validate_record/sanitize_people/_sanitize_action_texts, AND _normalize_meddpicc
# carries a prior element's narrative forward; sanitize_meddpicc must neutralise a
# fabricated person / placeholder there too (incl. one carried forward).

def test_sanitize_meddpicc_neutralises_unverifiable_person_in_narrative():
    ai = {"meddpicc": {
        "economic_buyer": {"status": "confirmed",
                           "narrative": "Ghost Exec (economic buyer) holds the budget.",
                           "sources": []}}}
    n = V.sanitize_meddpicc(ai, set(), _sf())
    assert n >= 1
    narr = ai["meddpicc"]["economic_buyer"]["narrative"]
    assert "Ghost Exec" not in narr
    assert V._PERSON_ROLE in narr


def test_sanitize_meddpicc_keeps_known_person_in_narrative():
    allow = {V._norm_name("Jane Buyer")}
    ai = {"meddpicc": {
        "champion": {"status": "confirmed",
                     "narrative": "Jane Buyer (champion) sponsors us daily.",
                     "sources": []}}}
    n = V.sanitize_meddpicc(ai, allow, _sf())
    assert n == 0
    assert "Jane Buyer" in ai["meddpicc"]["champion"]["narrative"]


def test_sanitize_meddpicc_scrubs_placeholder_and_marks_gap():
    ai = {"meddpicc": {
        "metrics": {"status": "confirmed", "narrative": "<id>", "sources": []}}}
    n = V.sanitize_meddpicc(ai, set(), _sf())
    assert n >= 1
    el = ai["meddpicc"]["metrics"]
    assert el["narrative"] == ""
    assert el["status"] == "gap"


def test_sanitize_meddpicc_leaves_evidence_names_in_sources():
    # A name inside a verbatim quote in `sources` is evidence, not an assertion.
    ai = {"meddpicc": {
        "champion": {"status": "confirmed",
                     "narrative": "A senior sponsor backs us.",
                     "sources": ["Avoma 4 Apr: 'I'll push this through' — Ghost Exec"]}}}
    n = V.sanitize_meddpicc(ai, set(), _sf())
    assert n == 0
    assert "Ghost Exec" in ai["meddpicc"]["champion"]["sources"][0]


def test_carried_forward_meddpicc_fabrication_is_neutralised():
    """End-to-end finding-3 path: this sweep emits an EMPTY narrative, so
    _normalize_meddpicc carries the prior (poisoned) one forward; sanitize_meddpicc
    run AFTER it must still neutralise the fabricated person."""
    from deal_engine_sweep import _normalize_meddpicc
    prior_ai = {"meddpicc": {
        "economic_buyer": {"status": "confirmed",
                           "narrative": "Ghost Exec (economic buyer) holds the budget.",
                           "sources": []}}}
    new_ai = {"meddpicc": {"economic_buyer": {"status": "gap", "narrative": "",
                                              "sources": []}}}
    _normalize_meddpicc(new_ai, prior_ai)
    # carried forward verbatim at this point...
    assert "Ghost Exec" in new_ai["meddpicc"]["economic_buyer"]["narrative"]
    # ...then the gate neutralises it.
    n = V.sanitize_meddpicc(new_ai, set(), _sf())
    assert n >= 1
    assert "Ghost Exec" not in new_ai["meddpicc"]["economic_buyer"]["narrative"]
