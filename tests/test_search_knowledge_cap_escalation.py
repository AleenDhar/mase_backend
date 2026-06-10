"""Tests for the search_knowledge cap-escalation hard-cancel
(added 2026-05-22 after chat 8359d7a6 burned $8.02 on a RAG loop).

The per-turn cap (MAX_SEARCH_KNOWLEDGE_PER_TURN=6) blocks individual
tool invocations, but Sonnet 4.6 ignores cap errors in tool_result and
keeps generating new search_knowledge tool_use blocks each LLM turn.
After MAX_SK_CAP_HITS_BEFORE_CANCEL repeated cap blocks we forcibly
cancel the agent loop via server.cancel_running_chat.
"""
import sys
import os
import types
import importlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _reload_sk_module(monkeypatch, cap=6, hits_before_cancel=3):
    """Reload custom_tools.search_knowledge with controlled env vars
    so tests are deterministic regardless of the host shell."""
    monkeypatch.setenv("MAX_SEARCH_KNOWLEDGE_PER_TURN", str(cap))
    monkeypatch.setenv("MAX_SK_CAP_HITS_BEFORE_CANCEL", str(hits_before_cancel))
    if "custom_tools.search_knowledge" in sys.modules:
        return importlib.reload(sys.modules["custom_tools.search_knowledge"])
    import custom_tools.search_knowledge as sk
    return sk


def _install_fake_server(cancel_calls):
    """Install a fake `server` module exposing cancel_running_chat,
    recording each call into the provided list."""
    fake = types.ModuleType("server")

    def cancel_running_chat(chat_id):  # noqa: ANN001, D401
        cancel_calls.append(chat_id)
        return True
    fake.cancel_running_chat = cancel_running_chat
    sys.modules["server"] = fake


_DISTINCT = [
    "alpha framework email",
    "bravo title prioritization",
    "charlie value proposition",
    "delta opening hooks",
    "echo case study procurement",
    "foxtrot routing card",
    "golf supplier metrics",
    "hotel campaign deliverable",
    "india content batch",
    "juliet draft template",
]


def test_under_cap_calls_pass(monkeypatch):
    sk = _reload_sk_module(monkeypatch, cap=6, hits_before_cancel=3)
    chat = "chat-A"
    sk.reset_search_knowledge_counter(chat)
    for i in range(6):
        assert sk._check_and_record_sk_call(chat, _DISTINCT[i]) is None


def test_cap_blocks_seventh_call(monkeypatch):
    sk = _reload_sk_module(monkeypatch, cap=6, hits_before_cancel=3)
    chat = "chat-B"
    sk.reset_search_knowledge_counter(chat)
    for i in range(6):
        sk._check_and_record_sk_call(chat, _DISTINCT[i])
    err = sk._check_and_record_sk_call(chat, _DISTINCT[6])
    assert err is not None
    assert "cap of 6" in err
    # Not yet escalated (only 1 cap-hit)
    assert sk._sk_cap_hits[chat] == 1


def test_duplicate_does_not_count_against_cancel_budget(monkeypatch):
    """Parallel tool_use siblings re-issuing the same query show up as
    duplicates — they must NOT push us toward the hard-cancel."""
    cancels = []
    _install_fake_server(cancels)
    sk = _reload_sk_module(monkeypatch, cap=6, hits_before_cancel=3)
    chat = "chat-C"
    sk.reset_search_knowledge_counter(chat)
    sk._check_and_record_sk_call(chat, "alpha")  # accepted
    for _ in range(20):
        err = sk._check_and_record_sk_call(chat, "alpha")
        assert "Duplicate" in err
    assert sk._sk_cap_hits.get(chat, 0) == 0
    assert cancels == []


def test_hard_cancel_fires_after_n_cap_hits(monkeypatch):
    cancels = []
    _install_fake_server(cancels)
    sk = _reload_sk_module(monkeypatch, cap=4, hits_before_cancel=3)
    chat = "chat-D"
    sk.reset_search_knowledge_counter(chat)
    # Fill the cap with unique queries (distinct content words so the
    # bag-of-words normaliser doesn't collapse them into duplicates).
    for i in range(4):
        sk._check_and_record_sk_call(chat, _DISTINCT[i])
    assert cancels == []
    # Cap-hit #1
    err1 = sk._check_and_record_sk_call(chat, _DISTINCT[4])
    assert "cap of 4" in err1
    assert "RUN TERMINATED" not in err1
    assert cancels == []
    # Cap-hit #2
    err2 = sk._check_and_record_sk_call(chat, _DISTINCT[5])
    assert "cap of 4" in err2
    assert cancels == []
    # Cap-hit #3 → escalation
    err3 = sk._check_and_record_sk_call(chat, _DISTINCT[6])
    assert "RUN TERMINATED" in err3
    assert cancels == [chat]
    # Subsequent cap-blocks for the same chat must NOT re-fire the cancel.
    # The error string downgrades back to the regular cap message because
    # the cancel already landed — the agent is being torn down, so the
    # exact tool_result text no longer matters.
    err4 = sk._check_and_record_sk_call(chat, _DISTINCT[7])
    assert "cap of 4" in err4
    assert cancels == [chat], "cancel must not fire twice for the same chat"


def test_reset_clears_cap_hits_and_cancelled_flag(monkeypatch):
    cancels = []
    _install_fake_server(cancels)
    sk = _reload_sk_module(monkeypatch, cap=2, hits_before_cancel=2)
    chat = "chat-E"
    sk.reset_search_knowledge_counter(chat)
    sk._check_and_record_sk_call(chat, _DISTINCT[0])
    sk._check_and_record_sk_call(chat, _DISTINCT[1])
    sk._check_and_record_sk_call(chat, _DISTINCT[2])  # cap-hit 1
    sk._check_and_record_sk_call(chat, _DISTINCT[3])  # cap-hit 2 → cancel
    assert cancels == [chat]
    # Reset wipes cap state
    sk.reset_search_knowledge_counter(chat)
    assert chat not in sk._sk_cap_hits
    assert chat not in sk._sk_cancelled
    # New run can fire its own cancel
    sk._check_and_record_sk_call(chat, _DISTINCT[4])
    sk._check_and_record_sk_call(chat, _DISTINCT[5])
    sk._check_and_record_sk_call(chat, _DISTINCT[6])  # cap-hit 1
    sk._check_and_record_sk_call(chat, _DISTINCT[7])  # cap-hit 2 → cancel #2
    assert cancels == [chat, chat]


def test_cancel_hook_import_failure_is_swallowed(monkeypatch):
    """If `server` cannot be imported we must still return an informative
    error string and not crash the agent. The error message must NOT
    claim 'RUN TERMINATED' because the run wasn't actually cancelled."""
    sys.modules.pop("server", None)
    # Make `from server import cancel_running_chat` fail
    sys.modules["server"] = types.ModuleType("server")  # no attr
    sk = _reload_sk_module(monkeypatch, cap=2, hits_before_cancel=2)
    chat = "chat-F"
    sk.reset_search_knowledge_counter(chat)
    sk._check_and_record_sk_call(chat, _DISTINCT[0])
    sk._check_and_record_sk_call(chat, _DISTINCT[1])
    sk._check_and_record_sk_call(chat, _DISTINCT[2])  # cap-hit 1
    err = sk._check_and_record_sk_call(chat, _DISTINCT[3])  # cap-hit 2 → cancel attempt
    assert "RUN TERMINATED" not in err
    assert "attempt to terminate the run failed" in err
    # Crucially: chat must NOT be in _sk_cancelled, so the next cap-hit
    # can retry the cancel (architect review fix #2 from 2026-05-22).
    assert chat not in sk._sk_cancelled


def test_failed_cancel_is_retried_on_next_cap_hit(monkeypatch):
    """If the first cancel attempt returns False, the next cap-hit must
    try again instead of silently leaving the runaway loop alive."""
    attempts = []
    # Fake server whose cancel returns False the first time, True the second.
    fake = types.ModuleType("server")
    call_count = {"n": 0}

    def cancel_running_chat(chat_id):  # noqa: ANN001
        call_count["n"] += 1
        attempts.append(chat_id)
        return call_count["n"] >= 2
    fake.cancel_running_chat = cancel_running_chat
    sys.modules["server"] = fake
    sk = _reload_sk_module(monkeypatch, cap=2, hits_before_cancel=2)
    chat = "chat-G"
    sk.reset_search_knowledge_counter(chat)
    sk._check_and_record_sk_call(chat, _DISTINCT[0])
    sk._check_and_record_sk_call(chat, _DISTINCT[1])
    sk._check_and_record_sk_call(chat, _DISTINCT[2])  # cap-hit 1
    # cap-hit 2 → first cancel attempt fails
    err1 = sk._check_and_record_sk_call(chat, _DISTINCT[3])
    assert attempts == [chat]
    assert chat not in sk._sk_cancelled
    assert "RUN TERMINATED" not in err1
    # cap-hit 3 → cancel retried, succeeds this time
    err2 = sk._check_and_record_sk_call(chat, _DISTINCT[4])
    assert attempts == [chat, chat]
    assert chat in sk._sk_cancelled
    assert "RUN TERMINATED" in err2
