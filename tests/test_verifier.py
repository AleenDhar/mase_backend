"""Pure-function tests for the verifier engine.

No Supabase. No network. We hand-craft `ToolCall` lists and assert the
verdict matches what the spec demands.
"""

from __future__ import annotations

from verifier.checker import (
    CheckResult,
    ExpectedCall,
    FlowSpec,
    ToolMatcher,
    args_contain,
    args_field_equals,
    evaluate_flow,
    render_verdict,
    verdict_to_dict,
)
from verifier.flow_detection import detect_flow_for_chat
from verifier.flows.abm_v11 import (
    ABM_PROJECT_IDS,
    ABM_V11_FLOW,
    check_seven_angles,
)
from verifier.loader import ToolCall


def _call(seq, tool, args, tcid=None):
    return ToolCall(
        sequence=seq,
        created_at=f"2026-05-13T00:00:{seq:02d}Z",
        tool=tool,
        args=args,
        tool_call_id=tcid,
        source="test",
    )


# ---------- ToolMatcher / ExpectedCall basics ---------------------------

def test_tool_matcher_name_only():
    m = ToolMatcher(tool_names=("foo", "bar"))
    assert m.matches(_call(1, "foo", {}))
    assert m.matches(_call(2, "bar", {"x": 1}))
    assert not m.matches(_call(3, "baz", {}))


def test_tool_matcher_arg_predicate():
    m = ToolMatcher(
        tool_names=("search_knowledge",),
        arg_predicate=args_contain("query", "EMAIL_FRAMEWORK"),
    )
    assert m.matches(_call(1, "search_knowledge", {"query": "load EMAIL_FRAMEWORK now"}))
    assert not m.matches(_call(2, "search_knowledge", {"query": "load value props"}))


def test_args_field_equals():
    pred = args_field_equals("object_api_name", "Account")
    assert pred({"object_api_name": "Account"})
    assert not pred({"object_api_name": "Contact"})


def test_expected_call_min_count_partial_vs_miss():
    exp = ExpectedCall(
        id="X",
        description="x",
        phase="P1",
        matchers=(ToolMatcher(tool_names=("foo",)),),
        min_count=3,
    )
    spec = FlowSpec(name="t", version="1", project_ids=("p",), expected=(exp,))
    # 0 calls -> miss
    v = evaluate_flow("c1", "p", [], spec)
    assert v.results[0].status == "miss"
    # 1 call -> partial
    v = evaluate_flow("c2", "p", [_call(1, "foo", {})], spec)
    assert v.results[0].status == "partial"
    # 3 calls -> pass
    v = evaluate_flow(
        "c3", "p",
        [_call(i, "foo", {}) for i in range(1, 4)],
        spec,
    )
    assert v.results[0].status == "pass"


# ---------- ABM v11 flow against synthetic happy-path -------------------

def _abm_happy_path_calls():
    """A synthetic clean ABM run hitting every expected call + every angle."""
    return [
        # P1+P2 batch
        _call(1, "get_record", {"object_api_name": "Account", "record_id": "0010O00002Lp2rN"}),
        _call(2, "soql", {"query": "SELECT Id FROM Contact WHERE AccountId='0010O00002Lp2rN'"}),
        _call(3, "soql", {"query": "SELECT Id FROM Opportunity WHERE AccountId='0010O00002Lp2rN'"}),
        _call(4, "soql", {"query": "SELECT Id FROM Task WHERE AccountId='0010O00002Lp2rN'"}),
        _call(5, "search_knowledge", {"query": "Title_Prioritization scoring rules"}),
        _call(6, "search_knowledge", {"query": "CIM v2.4 contact intelligence routing"}),
        # 7 angles
        _call(10, "web_search", {"query": "Macquarie Group CPO chief procurement officer 2026"}),
        _call(11, "web_search", {"query": "Macquarie Group head of procurement"}),
        _call(12, "web_search", {"query": "Macquarie Group head of sourcing strategic sourcing"}),
        _call(13, "web_search", {"query": "Macquarie Group CFO chief financial officer"}),
        _call(14, "web_search", {"query": "Macquarie Group procurement transformation"}),
        _call(15, "web_search", {"query": "site:linkedin.com/in Macquarie Group procurement"}),
        _call(16, "web_search", {"query": "site:linkedin.com/in Macquarie Group finance controller"}),
        # P3
        _call(20, "validate_batch", {"emails": ["a@b.com"]}),
        _call(21, "zi_enrich_contact", {"first": "Jane", "last": "Doe"}),
        # P4 RAG × 4
        _call(30, "search_knowledge", {"query": "EMAIL_FRAMEWORK structure"}),
        _call(31, "search_knowledge", {"query": "EMAIL_INTEL signals"}),
        _call(32, "search_knowledge", {"query": "VALUE_PROPOSITIONS by persona"}),
        _call(33, "search_knowledge", {"query": "CASE_STUDIES financial services"}),
        # P5
        _call(40, "lemlist_get_team", {}),
        _call(41, "lemlist_get_campaign", {"campaignId": "cam_1"}),
        _call(42, "lemlist_get_campaign_sequences", {"campaignId": "cam_1"}),
        _call(43, "lemlist_add_lead_to_campaign", {"campaignId": "cam_1", "email": "a@b.com"}),
        _call(44, "lemlist_get_lead_in_campaign", {"campaignId": "cam_1", "email": "a@b.com"}),
    ]


def test_abm_happy_path_clean():
    calls = _abm_happy_path_calls()
    v = evaluate_flow(
        "chat-happy", ABM_PROJECT_IDS[0], calls, ABM_V11_FLOW,
    )
    assert v.passed, (
        "Happy path should pass; missed: "
        + ", ".join(v.missed_ids)
        + "\n" + render_verdict(v)
    )
    # Every angle individually should pass
    angle_ids = [r.id for r in v.results if r.id.startswith("P1_ANGLE_")]
    assert len(angle_ids) == 7
    for r in v.results:
        if r.id.startswith("P1_ANGLE_"):
            assert r.status == "pass", f"{r.id} should pass"


def test_abm_two_web_searches_flags_six_angles_missing():
    """Reproduce the spike's Macquarie-style failure mode (the whole point)."""
    calls = [
        _call(1, "get_record", {"object_api_name": "Account", "record_id": "0010O00002Lp2rN"}),
        _call(2, "soql", {"query": "SELECT Id FROM Contact"}),
        _call(3, "soql", {"query": "SELECT Id FROM Opportunity"}),
        _call(4, "soql", {"query": "SELECT Id FROM Task"}),
        _call(10, "web_search", {"query": "Macquarie Group procurement Coupa renewal"}),
        _call(11, "web_search", {"query": "Macquarie Group procurement transformation GenAI"}),
    ]
    v = evaluate_flow("c-dirty", ABM_PROJECT_IDS[0], calls, ABM_V11_FLOW)
    assert not v.passed
    angle_misses = [r for r in v.results if r.id.startswith("P1_ANGLE_") and r.status == "miss"]
    # at most 1 angle could match (A5 procurement transformation); rest must miss
    assert len(angle_misses) >= 6, render_verdict(v)


def test_loader_dedupes_wrapper_row_when_id_bearing_row_present():
    """Production shape: same call appears 2-3 times across write paths.
    Wrapper write has no tool_call_id; sync/stream write does. The id-bearing
    row wins; the wrapper duplicate is dropped."""
    from verifier.loader import load_tool_calls

    class FakeSb:
        def __init__(self, rows):
            self._rows = rows

        def table(self, _name):
            return self

        def select(self, *_a, **_k): return self
        def eq(self, *_a, **_k): return self
        def order(self, *_a, **_k): return self

        def range(self, *_a, **_k):
            class R:
                def __init__(s, data): s.data = data
                def execute(s): return s
            return R(self._rows)

    rows = [
        # wrapper write (no tool_call_id)
        {"sequence": 10, "created_at": "t1", "metadata":
         {"tool": "soql", "args": {"query": "SELECT Id FROM Account"},
          "source": "tool_wrapper"}},
        # sync_handler write (with tool_call_id)
        {"sequence": 11, "created_at": "t2", "metadata":
         {"tool": "soql", "args": {"query": "SELECT Id FROM Account"},
          "source": "sync_handler", "tool_call_id": "call_xyz"}},
        # legitimate second call with different args — must not be dropped
        {"sequence": 12, "created_at": "t3", "metadata":
         {"tool": "soql", "args": {"query": "SELECT Id FROM Contact"},
          "source": "sync_handler", "tool_call_id": "call_abc"}},
    ]
    calls = load_tool_calls("c", supabase=FakeSb(rows))
    assert len(calls) == 2
    assert {c.tool_call_id for c in calls} == {"call_xyz", "call_abc"}


def test_dedupe_three_writes_are_collapsed_by_tool_call_id():
    """server.py logs the same call from 3 paths; loader dedupes by tool_call_id."""
    # Simulate by creating ToolCalls with the same id — evaluate_flow
    # treats them all, but real loader.load_tool_calls dedupes upstream.
    # Here we just sanity-check the engine isn't double-counting on its own.
    exp = ExpectedCall(
        id="X", description="", phase="P", min_count=2,
        matchers=(ToolMatcher(tool_names=("foo",)),),
    )
    spec = FlowSpec(name="t", version="1", project_ids=("p",), expected=(exp,))
    calls = [_call(1, "foo", {}, tcid="abc")]  # 1 unique call
    v = evaluate_flow("c", "p", calls, spec)
    assert v.results[0].observed == 1
    assert v.results[0].status == "partial"


def test_flow_detection():
    assert detect_flow_for_chat(ABM_PROJECT_IDS[0]) is ABM_V11_FLOW
    assert detect_flow_for_chat("00000000-0000-0000-0000-000000000000") is None
    assert detect_flow_for_chat(None) is None


def test_verdict_to_dict_is_json_safe():
    import json
    calls = _abm_happy_path_calls()
    v = evaluate_flow("c", ABM_PROJECT_IDS[0], calls, ABM_V11_FLOW)
    d = verdict_to_dict(v)
    json.dumps(d)  # must not raise
    assert d["flow"] == "abm_v11"
    assert d["passed"] is True
    assert isinstance(d["results"], list)


def test_seven_angles_account_token_required():
    """Web searches without account context don't satisfy any angle."""
    calls = [
        _call(1, "web_search", {"query": "chief procurement officer 2026 trends"}),
        _call(2, "web_search", {"query": "head of sourcing best practices"}),
    ]
    results = check_seven_angles(calls, {})
    # all angles miss because no account_tokens derivable
    angle_results = [r for r in results if r.id.startswith("P1_ANGLE_")]
    assert all(r.status == "miss" for r in angle_results)


def test_remediation_prompt_lists_missed_checks_grouped_by_phase():
    from verifier.remediation import build_remediation_prompt
    calls = [
        _call(1, "get_record", {"object_api_name": "Account", "record_id": "x"}),
        _call(2, "soql", {"query": "SELECT Id FROM Contact"}),
        _call(3, "soql", {"query": "SELECT Id FROM Opportunity"}),
        _call(4, "soql", {"query": "SELECT Id FROM Task"}),
        _call(5, "web_search", {"query": "Acme procurement transformation"}),
    ]
    v = evaluate_flow("c", ABM_PROJECT_IDS[0], calls, ABM_V11_FLOW)
    prompt = build_remediation_prompt(v)
    assert "Coverage check" in prompt
    # phase headings present (only phases with `expected`-severity gaps)
    assert "P1+P2 (Researcher + Mapper)" in prompt
    assert "P4 (Writer)" in prompt
    assert "P3 (Strategist)" in prompt
    # specific missed IDs surfaced
    assert "P4_RAG_EMAIL_FRAMEWORK" in prompt
    assert "P3_ENRICHMENT_WATERFALL" in prompt
    assert "P1_ANGLE_A1_CPO" in prompt
    # advisory-severity checks (P5 Lemlist, P1 RAG preloads) are NOT
    # surfaced as required remediation
    assert "P5_LEMLIST_GET_TEAM" not in prompt
    assert "P1_RAG_TITLE_PRIORITIZATION" not in prompt


def test_remediation_prompt_empty_when_passed():
    from verifier.remediation import build_remediation_prompt
    v = evaluate_flow("c", ABM_PROJECT_IDS[0], _abm_happy_path_calls(), ABM_V11_FLOW)
    assert v.passed
    assert build_remediation_prompt(v) == ""


def test_should_remediate_caps_at_one_attempt():
    from verifier.remediation import should_remediate, MAX_REMEDIATION_ATTEMPTS

    class FakeSb:
        def __init__(self, prior_count):
            self._count = prior_count
        def table(self, _n): return self
        def select(self, *_a, **_k): return self
        def eq(self, *_a, **_k): return self
        def execute(self):
            class R: pass
            r = R(); r.count = self._count; r.data = []
            return r

    # build a dirty verdict
    v = evaluate_flow("c", ABM_PROJECT_IDS[0],
                      [_call(1, "web_search", {"query": "x"})],
                      ABM_V11_FLOW)
    assert not v.passed
    assert should_remediate(v, FakeSb(0)) is True
    assert should_remediate(v, FakeSb(MAX_REMEDIATION_ATTEMPTS)) is False
    assert should_remediate(v, FakeSb(99)) is False


def test_next_sequence_returns_max_plus_one():
    from verifier.loader import next_sequence

    class FakeSb:
        def __init__(self, rows): self._rows = rows
        def table(self, _n): return self
        def select(self, *_a, **_k): return self
        def eq(self, *_a, **_k): return self
        def order(self, *_a, **_k): return self
        def limit(self, *_a, **_k): return self
        def execute(self):
            class R: pass
            r = R(); r.data = self._rows; return r

    assert next_sequence("c", FakeSb([{"sequence": 42}])) == 43
    assert next_sequence("c", FakeSb([])) == 1
    assert next_sequence("c", FakeSb([{"sequence": None}])) == 1
    assert next_sequence("c", FakeSb([{"sequence": 0}])) == 1


def test_save_to_supabase_resumes_sequence_after_counter_pop():
    """Regression for task #26 — exercises the real server.py code path.

    Bug: when `_supabase_seq_counters[chat_id]` is missing (remediation
    re-run, session-cleanup pop, server restart), lazy-init started the
    counter at 0 and second-turn writes landed BELOW the UI's current_max,
    so the realtime feed dropped them as "older than current_max".

    This test imports server.py and calls the actual `save_to_supabase`
    function with a fake supabase client, asserting that:
      1. First insert seeds from DB max (75) -> seq=76.
      2. After the counter is popped (mimicking session cleanup), the next
         insert resumes past the existing rows, not from 1.
      3. The standalone `_query_max_sequence` helper handles None client
         and empty results.
    """
    import asyncio
    from unittest import mock
    import server

    captured = []
    db_max = {"value": 75}

    class FakeTable:
        def insert(self, payload):
            captured.append(payload)
            db_max["value"] = max(db_max["value"], payload.get("sequence", 0))
            return self
        def select(self, *_a, **_k): return self
        def eq(self, *_a, **_k): return self
        def order(self, *_a, **_k): return self
        def limit(self, *_a, **_k): return self
        def execute(self):
            class R: pass
            r = R()
            r.data = [{"sequence": db_max["value"]}]
            return r

    class FakeSb:
        def table(self, _n): return FakeTable()

    chat_id = "test-chat-resume"
    with mock.patch.object(server, "supabase", FakeSb()):
        # Pretend chat row already exists so ensure_chat_row is a no-op.
        server._chats_created.add(chat_id)
        server._supabase_seq_counters.pop(chat_id, None)

        # First insert: counter missing, must seed from DB max=75 -> seq=76.
        asyncio.run(server.save_to_supabase(chat_id, "tool_call", "first"))
        first_seq = captured[-1]["sequence"]
        assert first_seq == 76, f"expected 76 (DB max 75 + 1), got {first_seq}"

        # Mimic session-cleanup pop between turns.
        server._supabase_seq_counters.pop(chat_id, None)

        # Second insert: counter missing again, must resume from DB max,
        # NOT restart at 1.
        asyncio.run(server.save_to_supabase(chat_id, "tool_call", "second"))
        second_seq = captured[-1]["sequence"]
        assert second_seq > first_seq, (
            f"after counter pop, second seq must continue past first "
            f"({first_seq}), got {second_seq} — would be dropped by UI feed"
        )

        # Third insert without a pop: monotonic via in-memory counter.
        asyncio.run(server.save_to_supabase(chat_id, "tool_call", "third"))
        third_seq = captured[-1]["sequence"]
        assert third_seq == second_seq + 1

        # Direct helper sanity checks.
        assert server._query_max_sequence(chat_id) == third_seq

    # Helper handles None supabase gracefully.
    with mock.patch.object(server, "supabase", None):
        assert server._query_max_sequence(chat_id) == 0

    # Cleanup so we don't leak state into other tests.
    server._chats_created.discard(chat_id)
    server._supabase_seq_counters.pop(chat_id, None)


def test_mark_remediation_inserts_with_sequence():
    """Regression: verifier rows MUST include a sequence so the UI live-feed
    sees them (else they default to 0 and only appear after page reload)."""
    from verifier.remediation import mark_remediation

    captured = {"insert_payload": None}

    class FakeSb:
        def __init__(self):
            self.rows = []
        def table(self, _n): return self
        def insert(self, payload):
            captured["insert_payload"] = payload
            self.rows.append({"metadata": payload["metadata"], "created_at": "t1"})
            return self
        def select(self, *_a, **_k): return self
        def eq(self, *_a, **_k): return self
        def order(self, *_a, **_k): return self
        def limit(self, *_a, **_k): return self
        def execute(self):
            class R: pass
            r = R()
            # Return existing rows for both next_sequence + claim-check queries
            r.data = sorted(self.rows, key=lambda x: x.get("created_at", ""))[:1] if self.rows else []
            return r

    v = evaluate_flow("c", ABM_PROJECT_IDS[0],
                      [_call(1, "web_search", {"query": "x"})], ABM_V11_FLOW)
    sb = FakeSb()
    mark_remediation(v, "prompt", sb)
    assert captured["insert_payload"] is not None
    assert "sequence" in captured["insert_payload"]
    assert captured["insert_payload"]["sequence"] >= 1


def test_mark_remediation_wins_when_only_claim():
    from verifier.remediation import mark_remediation

    state = {"rows": []}

    class FakeSb:
        def table(self, _n): return self
        def insert(self, payload):
            state["rows"].append({"metadata": payload["metadata"], "created_at": f"t{len(state['rows'])}"})
            return self
        def select(self, *_a, **_k): return self
        def eq(self, *_a, **_k): return self
        def order(self, *_a, **_k): return self
        def limit(self, *_a, **_k): return self
        def execute(self):
            class R: pass
            r = R()
            r.data = sorted(state["rows"], key=lambda x: x["created_at"])[:1] if state["rows"] else []
            r.count = len(state["rows"])
            return r

    v = evaluate_flow("c", ABM_PROJECT_IDS[0],
                      [_call(1, "web_search", {"query": "x"})], ABM_V11_FLOW)
    sb = FakeSb()
    assert mark_remediation(v, "prompt", sb) is True


def test_mark_remediation_loses_race_to_earlier_claim():
    """Simulate a competing concurrent task that already inserted first."""
    from verifier.remediation import mark_remediation
    import json

    class FakeSb:
        def __init__(self):
            # Pre-existing earlier row from a competing task
            self.rows = [{
                "metadata": json.dumps({"claim_id": "earlier-other-task"}),
                "created_at": "t0",
            }]
        def table(self, _n): return self
        def insert(self, payload):
            self.rows.append({"metadata": payload["metadata"], "created_at": "t1"})
            return self
        def select(self, *_a, **_k): return self
        def eq(self, *_a, **_k): return self
        def order(self, *_a, **_k): return self
        def limit(self, *_a, **_k): return self
        def execute(self):
            class R: pass
            r = R()
            r.data = sorted(self.rows, key=lambda x: x["created_at"])[:1]
            r.count = len(self.rows)
            return r

    v = evaluate_flow("c", ABM_PROJECT_IDS[0],
                      [_call(1, "web_search", {"query": "x"})], ABM_V11_FLOW)
    assert mark_remediation(v, "prompt", FakeSb()) is False


def test_should_remediate_skips_clean_verdict():
    from verifier.remediation import should_remediate

    class FakeSb:
        def table(self, _n): return self
        def select(self, *_a, **_k): return self
        def eq(self, *_a, **_k): return self
        def execute(self):
            class R: pass
            r = R(); r.count = 0; r.data = []
            return r

    v = evaluate_flow("c", ABM_PROJECT_IDS[0], _abm_happy_path_calls(), ABM_V11_FLOW)
    assert v.passed
    assert should_remediate(v, FakeSb()) is False


def test_seven_angles_with_explicit_account_token():
    calls = [
        _call(1, "web_search", {"query": "Acme Corp chief procurement officer"}),
        _call(2, "web_search", {"query": "site:linkedin.com/in Acme Corp procurement"}),
    ]
    results = check_seven_angles(calls, {"extra_account_tokens": ["acme"]})
    by_id = {r.id: r for r in results if r.id.startswith("P1_ANGLE_")}
    assert by_id["P1_ANGLE_A1_CPO"].status == "pass"
    assert by_id["P1_ANGLE_A6_LINKEDIN_PROCUREMENT"].status == "pass"
    assert by_id["P1_ANGLE_A4_CFO"].status == "miss"
