"""Tests for the circuit-breaker detection added to
pipeline_runner._run_phase on 2026-05-22 (chat 8359d7a6).

When server.py's auto-continuation circuit-breaker fires
(budget_continuations / budget_cost / budget_time) mid-phase, the agent
still produces a long "thinking process" final text. Before this fix
_run_phase saw the non-empty text and marked outcome='ok', so the
driver wrote pipeline_complete — lying to the UI about a half-run
pipeline. The new code scans chat_messages for the breaker error row
during the phase window and downgrades outcome to 'error'.
"""
import asyncio
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pipeline_runner  # noqa: E402


# --------------------------------------------------------------------------- helpers
class FakeExec:
    def __init__(self, rows):
        self.data = rows


class FakeChain:
    """Minimal supabase-py builder chain that records the column filters
    and returns a canned row set on .execute()."""

    def __init__(self, rows):
        self._rows = rows
        self.filters = {}

    def select(self, *_a, **_kw):
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def in_(self, col, vals):
        self.filters[f"{col}__in"] = list(vals)
        return self

    def gte(self, col, val):
        self.filters[f"{col}__gte"] = val
        return self

    def order(self, *_a, **_kw):
        return self

    def execute(self):
        return FakeExec(self._rows)


class FakeSB:
    """Returns the chat_messages chain we want; other tables irrelevant
    because _run_phase only queries chat_messages in the breaker scan."""

    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def table(self, name):
        self.calls.append(name)
        return FakeChain(self._rows)


def _build_req(chat="chat-X"):
    """Construct the minimum RunPipelineRequest we need for _run_phase."""
    return pipeline_runner.RunPipelineRequest(
        chat_id=chat,
        project_id="proj-1",
        agent_chat_url="http://localhost:5000/api/chat",
        supabase_url="http://stub.supabase",
        supabase_service_key="stub-key",
        phases=[_build_phase()],
    )


def _build_phase():
    return pipeline_runner.PhaseSpec(
        id="ph-1", position=1, name="Phase 1",
        model_id="anthropic:x", system_prompt="",
    )


def _patch_runner_io(monkeypatch, phase_text):
    """Stub the per-phase I/O that we don't want to actually do:
    _stream_phase, _insert_row, _tag_phase_rows, _snapshot_chat_usage,
    _mark_task_phase_progress."""

    async def fake_stream(*_a, **_kw):
        return phase_text

    monkeypatch.setattr(pipeline_runner, "_stream_phase", fake_stream)
    monkeypatch.setattr(pipeline_runner, "_insert_row", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_runner, "_tag_phase_rows", lambda *a, **kw: 0)
    monkeypatch.setattr(
        pipeline_runner, "_snapshot_chat_usage",
        lambda *a, **kw: {"input_tokens": 0, "output_tokens": 0,
                          "total_tokens": 0, "cost_usd": 0.0},
    )
    monkeypatch.setattr(
        pipeline_runner, "_mark_task_phase_progress", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        pipeline_runner, "_mark_task_phase_complete", lambda *a, **kw: None
    )
    monkeypatch.setattr(pipeline_runner, "_maybe_loopback", lambda u: u)


# --------------------------------------------------------------------------- tests
def test_phase_ok_when_no_breaker_row(monkeypatch):
    """Baseline: non-empty text + zero breaker rows → outcome=ok."""
    sb = FakeSB(rows=[])
    _patch_runner_io(monkeypatch, phase_text="Phase 1 complete deliverable.")
    result = asyncio.run(pipeline_runner._run_phase(
        sb, _build_req(), _build_phase(), index=1, total=1, prior_outputs=[]
    ))
    assert result["outcome"] == "ok"
    assert result["error"] == ""


def test_phase_downgraded_on_budget_continuations_breaker(monkeypatch):
    """Long final text + chat_messages contains a budget_exhausted
    error row → outcome=error with circuit_breaker:<reason>."""
    breaker_row = {
        "metadata": {
            "status": "budget_exhausted",
            "terminal": {"reason": "budget_continuations",
                         "auto_continue_count": 25,
                         "cost_usd": 8.02,
                         "elapsed_s": 560},
        },
        "content": "Run hit circuit-breaker 'budget_continuations' after 25 ...",
    }
    sb = FakeSB(rows=[breaker_row])
    _patch_runner_io(monkeypatch, phase_text=("thinking process " * 200))
    result = asyncio.run(pipeline_runner._run_phase(
        sb, _build_req(), _build_phase(), index=1, total=1, prior_outputs=[]
    ))
    assert result["outcome"] == "error"
    assert result["error"] == "circuit_breaker:budget_continuations"


def test_phase_downgraded_on_budget_cost_breaker(monkeypatch):
    breaker_row = {
        "metadata": {
            "status": "budget_exhausted",
            "terminal": {"reason": "budget_cost", "cost_usd": 6.05},
        },
        "content": "Run hit circuit-breaker 'budget_cost' ...",
    }
    sb = FakeSB(rows=[breaker_row])
    _patch_runner_io(monkeypatch, phase_text="final dump")
    result = asyncio.run(pipeline_runner._run_phase(
        sb, _build_req(), _build_phase(), index=1, total=1, prior_outputs=[]
    ))
    assert result["outcome"] == "error"
    assert result["error"] == "circuit_breaker:budget_cost"


def test_phase_downgraded_when_metadata_is_json_string(monkeypatch):
    """Some supabase clients return metadata as a JSON string instead
    of a parsed dict — the scan must handle both shapes."""
    import json as _json
    breaker_row = {
        "metadata": _json.dumps({
            "status": "budget_exhausted",
            "terminal": {"reason": "budget_time"},
        }),
        "content": "...",
    }
    sb = FakeSB(rows=[breaker_row])
    _patch_runner_io(monkeypatch, phase_text="final dump")
    result = asyncio.run(pipeline_runner._run_phase(
        sb, _build_req(), _build_phase(), index=1, total=1, prior_outputs=[]
    ))
    assert result["outcome"] == "error"
    assert result["error"] == "circuit_breaker:budget_time"


def test_phase_ignores_unrelated_error_rows(monkeypatch):
    """A random tool error row (not a breaker) must NOT trip the
    downgrade — only budget_exhausted / known reasons count."""
    unrelated = {
        "metadata": {"tool": "soql", "error": "X"},
        "content": "soql failed",
    }
    sb = FakeSB(rows=[unrelated])
    _patch_runner_io(monkeypatch, phase_text="ok phase output")
    result = asyncio.run(pipeline_runner._run_phase(
        sb, _build_req(), _build_phase(), index=1, total=1, prior_outputs=[]
    ))
    assert result["outcome"] == "ok"
    assert result["error"] == ""


def test_empty_text_still_downgrades_to_error_without_breaker(monkeypatch):
    """Regression guard for the prior Task #33 fix — empty text alone
    still becomes outcome=error with error='empty_phase_output'."""
    sb = FakeSB(rows=[])
    _patch_runner_io(monkeypatch, phase_text="   ")
    result = asyncio.run(pipeline_runner._run_phase(
        sb, _build_req(), _build_phase(), index=1, total=1, prior_outputs=[]
    ))
    assert result["outcome"] == "error"
    assert result["error"] == "empty_phase_output"
