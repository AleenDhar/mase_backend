"""Smoke tests for Task #33 pipeline_runner timeout fixes.

Mocks the external boundaries (Supabase client, _stream_phase HTTP call,
server.cancel_running_chat hook) so we can assert the driver-level branching
on phase outcomes without spinning up the full server. Replays the failure
shape from chat d1d9260d (single phase that runs past the wall-clock cap).
"""
from __future__ import annotations

import asyncio
import sys
import types
from typing import Any, Dict, List

import pytest


# ---------------------------------------------------------------------------
# Supabase stub
# ---------------------------------------------------------------------------
class _Result:
    def __init__(self, data=None):
        self.data = data or []


class _Query:
    def __init__(self, sink, table):
        self.sink = sink
        self.table = table
        self._select = None
        self._payload = None
        self._eq = []
        self._gte = None
        self._limit = None
        self._op = None

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def select(self, cols):
        self._op = "select"
        self._select = cols
        return self

    def eq(self, col, val):
        self._eq.append((col, val))
        return self

    def gte(self, col, val):
        self._gte = (col, val)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        self.sink.append({"table": self.table, "op": self._op,
                          "payload": self._payload, "eq": list(self._eq)})
        # _tag_phase_rows SELECTs assistant rows — return [] so it's a no-op.
        # _snapshot_chat_usage SELECTs chat_usage — return [] so it returns zeros.
        # automation_tasks SELECTs phase_outputs — return [{"phase_outputs":[]}].
        if self._op == "select":
            if self.table == "automation_tasks":
                return _Result([{"phase_outputs": []}])
            return _Result([])
        return _Result([])


class _SBClient:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def table(self, name):
        return _Query(self.calls, name)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_server(monkeypatch):
    """Inject a stub `server` module exposing cancel_running_chat so the lazy
    `from server import cancel_running_chat` inside _run_phase works in
    isolation, and we can observe whether it was called."""
    log = {"calls": []}

    def _cancel(chat_id):
        log["calls"].append(chat_id)
        return True

    fake = types.ModuleType("server")
    fake.cancel_running_chat = _cancel
    monkeypatch.setitem(sys.modules, "server", fake)
    return log


@pytest.fixture
def pipeline_runner(monkeypatch):
    import pipeline_runner as pr
    # Speed up the post-cancel grace sleep so each test runs fast.
    return pr


def _req(pr, **overrides):
    base = dict(
        chat_id="chat-test",
        project_id="proj-test",
        shared_system_prefix="",
        messages=[{"role": "user", "content": "hi"}],
        phases=[pr.PhaseSpec(
            id="p1", position=1, name="Phase 1",
            model_id="anthropic:claude-sonnet-4-6",
            system_prompt="do the thing", enabled=True,
        )],
        api_keys={},
        agent_chat_url="http://example.invalid/api/chat",
        supabase_url="http://sb.invalid",
        supabase_service_key="key",
    )
    base.update(overrides)
    return pr.RunPipelineRequest(**base)


def _rows_of_type(sb: _SBClient, msg_type: str) -> List[Dict[str, Any]]:
    """All chat_messages insert payloads with the given type."""
    out = []
    for c in sb.calls:
        if c["table"] != "chat_messages" or c["op"] != "insert":
            continue
        payload = c["payload"] or {}
        if payload.get("type") == msg_type:
            out.append(payload)
    return out


def _task_updates(sb: _SBClient) -> List[Dict[str, Any]]:
    """All automation_tasks UPDATE payloads recorded by the fake client."""
    return [c["payload"] for c in sb.calls
            if c["table"] == "automation_tasks" and c["op"] == "update"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_phase_timeout_writes_partial_and_cancels(monkeypatch, fake_server, pipeline_runner):
    """Replays chat d1d9260d: one PhaseSpec, agent never finishes within the
    cap. Asserts the new behaviour:
      (a) `error` row with kind=phase_error,error=timeout is written
      (b) server.cancel_running_chat is called with the chat_id
      (c) terminal pair is pipeline_failed (1 phase, 0 ok), NOT pipeline_complete
      (d) no `pipeline_complete` rows are emitted
    """
    pr = pipeline_runner

    async def _stuck_stream(*args, **kwargs):
        # Sleep longer than the cap so wait_for trips.
        await asyncio.sleep(3.0)
        return "should not get here"

    sb = _SBClient()
    monkeypatch.setattr(pr, "_sb_client", lambda req: sb)
    monkeypatch.setattr(pr, "_stream_phase", _stuck_stream)

    req = _req(pr, phase_timeout_seconds=1)  # 1s cap

    asyncio.run(pr._run_pipeline(req))

    errors = _rows_of_type(sb, "error")
    finals = _rows_of_type(sb, "final")
    statuses = _rows_of_type(sb, "status")

    # (a) phase_error row written with timeout
    phase_errors = [e for e in errors if (e.get("metadata") or {}).get("kind") == "phase_error"]
    assert len(phase_errors) == 1
    assert phase_errors[0]["metadata"]["error"] == "timeout"
    assert phase_errors[0]["metadata"]["timeout_seconds"] == 1

    # (b) cancel hook was called
    assert fake_server["calls"] == [req.chat_id], (
        f"cancel_running_chat should have been called once for {req.chat_id}, "
        f"got {fake_server['calls']}"
    )

    # (c) terminal is an `error` row with kind=pipeline_failed (task #49: the
    #     failure terminal is a single `error`, not a `final`).
    pipeline_failed_terminals = [
        e for e in errors
        if (e.get("metadata") or {}).get("kind") == "pipeline_failed"
    ]
    assert len(pipeline_failed_terminals) == 1
    md = pipeline_failed_terminals[0]["metadata"]
    assert md["phases_ok"] == 0
    assert md["phases_failed"] == 1
    assert md["phase_outcomes"][0]["outcome"] == "timeout"

    # (d) NO pipeline_complete anywhere
    complete = [r for r in (errors + finals + statuses)
                if (r.get("metadata") or {}).get("kind") == "pipeline_complete"]
    assert complete == [], f"unexpected pipeline_complete rows: {complete}"


def test_two_phase_one_timeout_reports_partial(monkeypatch, fake_server, pipeline_runner):
    """Two phases: first succeeds, second times out. Expect pipeline_partial
    with ok=1, failed=1 and per-phase outcomes preserved."""
    pr = pipeline_runner

    call_count = {"n": 0}

    async def _mixed_stream(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "phase 1 output"
        await asyncio.sleep(3.0)
        return "never"

    sb = _SBClient()
    monkeypatch.setattr(pr, "_sb_client", lambda req: sb)
    monkeypatch.setattr(pr, "_stream_phase", _mixed_stream)

    phases = [
        pr.PhaseSpec(id="p1", position=1, name="Stage A",
                     model_id="x", system_prompt="a", enabled=True),
        pr.PhaseSpec(id="p2", position=2, name="Stage B",
                     model_id="x", system_prompt="b", enabled=True),
    ]
    req = _req(pr, phases=phases, phase_timeout_seconds=1)

    asyncio.run(pr._run_pipeline(req))

    # Task #49: the partial-run terminal is a single `error` row, not a `final`.
    errors = _rows_of_type(sb, "error")
    partial = [e for e in errors
               if (e.get("metadata") or {}).get("kind") == "pipeline_partial"]
    assert len(partial) == 1
    md = partial[0]["metadata"]
    assert md["phases_ok"] == 1
    assert md["phases_failed"] == 1
    outcomes = md["phase_outcomes"]
    assert outcomes[0]["outcome"] == "ok"
    assert outcomes[1]["outcome"] == "timeout"

    # cancel hook fired once (for the second/timing-out phase only)
    assert fake_server["calls"] == [req.chat_id]


def test_success_path_unchanged(monkeypatch, fake_server, pipeline_runner):
    """Single phase that returns text within the cap. Must still emit a
    pipeline_complete terminal `final` (no regression on the happy path) and
    must NOT call the cancel hook. Task #49: the terminal `final` is only
    emitted for automation runs (task_id set), so this run carries one."""
    pr = pipeline_runner

    async def _fast_stream(*args, **kwargs):
        return "phase 1 deliverable"

    sb = _SBClient()
    monkeypatch.setattr(pr, "_sb_client", lambda req: sb)
    monkeypatch.setattr(pr, "_stream_phase", _fast_stream)

    req = _req(pr, phase_timeout_seconds=5, task_id="t-success")
    asyncio.run(pr._run_pipeline(req))

    finals = _rows_of_type(sb, "final")
    complete = [f for f in finals
                if (f.get("metadata") or {}).get("kind") == "pipeline_complete"]
    assert len(complete) == 1
    assert complete[0]["metadata"]["phases_run"] == 1
    assert fake_server["calls"] == []


def test_failed_run_flips_automation_task_to_failed(
        monkeypatch, fake_server, pipeline_runner):
    """A run with a task_id whose only phase times out must flip the
    automation_tasks row to status='failed' using the real `error` column
    (NOT the non-existent `last_error`, which made Postgres reject the whole
    UPDATE and left every failed task stuck on 'running'). Exactly one terminal
    flip — the `finally` safety net must not double-flip."""
    pr = pipeline_runner

    async def _stuck(*args, **kwargs):
        await asyncio.sleep(3.0)
        return "nope"

    sb = _SBClient()
    monkeypatch.setattr(pr, "_sb_client", lambda req: sb)
    monkeypatch.setattr(pr, "_stream_phase", _stuck)

    req = _req(pr, phase_timeout_seconds=1, task_id="t-fail")
    asyncio.run(pr._run_pipeline(req))

    updates = _task_updates(sb)
    failed = [u for u in updates if u.get("status") == "failed"]
    assert failed, f"task never flipped to failed: {updates}"
    assert "error" in failed[0], "must write the real `error` column"
    assert all("last_error" not in u for u in updates), \
        "must never write the non-existent last_error column"
    terminal = [u for u in updates
                if u.get("status") in ("failed", "completed", "stopped")]
    assert len(terminal) == 1, f"expected exactly one terminal flip: {terminal}"


def test_success_run_flips_automation_task_to_completed(
        monkeypatch, fake_server, pipeline_runner):
    """A clean run with a task_id flips the row to status='completed' exactly
    once (no finally-net double-flip)."""
    pr = pipeline_runner

    async def _ok(*args, **kwargs):
        return "done"

    sb = _SBClient()
    monkeypatch.setattr(pr, "_sb_client", lambda req: sb)
    monkeypatch.setattr(pr, "_stream_phase", _ok)

    req = _req(pr, phase_timeout_seconds=5, task_id="t-ok")
    asyncio.run(pr._run_pipeline(req))

    updates = _task_updates(sb)
    completed = [u for u in updates if u.get("status") == "completed"]
    assert completed, f"task never flipped to completed: {updates}"
    terminal = [u for u in updates
                if u.get("status") in ("failed", "completed", "stopped")]
    assert len(terminal) == 1, f"expected exactly one terminal flip: {terminal}"


def test_phase_timeout_default_when_unset(monkeypatch, fake_server, pipeline_runner):
    """phase_timeout_seconds omitted/None → falls back to PHASE_TIMEOUT_SECONDS."""
    pr = pipeline_runner
    captured = {}

    async def _capture(agent_url, payload, read_timeout=None):
        captured["read_timeout"] = read_timeout
        return "ok"

    sb = _SBClient()
    monkeypatch.setattr(pr, "_sb_client", lambda req: sb)
    monkeypatch.setattr(pr, "_stream_phase", _capture)
    monkeypatch.setattr(pr, "PHASE_TIMEOUT_SECONDS", 1800)

    req = _req(pr)  # no override
    asyncio.run(pr._run_pipeline(req))
    assert captured["read_timeout"] == 1800
