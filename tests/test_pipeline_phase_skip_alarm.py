"""Regression guard for task #32 (phase-skip alarm + caller-side validation).

Covers three branches of _run_pipeline:
  1. Full run — all submitted phases enabled, no alarm fires.
  2. Cooperative stop — stop_requested mid-run, terminal `final` row written
     (existing behaviour from task #31, asserted here too as a tripwire).
  3. Phase-skip alarm — some submitted phases disabled, post-run `error`
     row with kind=pipeline_phases_skipped fires.
  4. Early rejection — caller declares expected_phases_min and submits fewer
     enabled phases; the runner refuses to start and emits the alarm + final.

Uses an in-memory fake supabase client so we don't need real network access.
"""
from __future__ import annotations

import asyncio
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pipeline_runner as pr


class _FakeTableExec:
    def __init__(self, recorder, op, payload):
        self.recorder = recorder
        self.op = op
        self.payload = payload

    def execute(self):
        self.recorder.append((self.op, self.payload))
        # Task #49: _insert_row now treats a response whose `.data` is None as a
        # dropped insert and retries/drops it. Return the inserted row as data so
        # the insert is counted as confirmed (mirrors real postgrest).
        r = _Result()
        r.data = [self.payload]
        return r


class _Result:
    data = None


class _FakeTable:
    def __init__(self, recorder, name):
        self.recorder = recorder
        self.name = name
        self._select = None
        self._filters = []

    def insert(self, row):
        return _FakeTableExec(self.recorder, ("insert", self.name), row)

    def update(self, row):
        # Return chainable object that records on execute
        outer = self

        class _Upd:
            def __init__(self):
                self._eq = []

            def eq(self, col, val):
                self._eq.append((col, val))
                return self

            def execute(self_inner):
                outer.recorder.append(
                    ("update", outer.name, row, list(self_inner._eq))
                )
                return self_inner
        return _Upd()

    def select(self, cols):
        self._select = cols
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def gte(self, col, val):
        self._filters.append((col, val, "gte"))
        return self

    def execute(self):
        # Return empty result for any tag-phase query.
        class R:
            data = []
        return R()


class _FakeSupabase:
    def __init__(self):
        self.recorder = []
        # automation_tasks rows keyed by task_id for stop_requested polling.
        self.tasks = {}

    def table(self, name):
        if name == "automation_tasks":
            # Custom path so _check_stop_requested can read tasks dict.
            return _AutomationTasksTable(self)
        return _FakeTable(self.recorder, name)


class _AutomationTasksTable:
    def __init__(self, sb):
        self.sb = sb
        self._eq = None

    def select(self, cols):
        return self

    def eq(self, col, val):
        self._eq = (col, val)
        return self

    def limit(self, n):
        return self

    def single(self):
        return self

    def execute(self):
        col, val = self._eq
        row = self.sb.tasks.get(val)

        class R:
            def __init__(self, data):
                self.data = data
        # _check_stop_requested expects res.data to be a list of rows.
        return R([row] if row else [])

    def update(self, row):
        sb = self.sb
        outer_eq = self._eq

        class _Upd:
            def __init__(self):
                self._eq = []

            def eq(self, col, val):
                self._eq.append((col, val))
                return self

            def execute(self_inner):
                if self_inner._eq:
                    _, tid = self_inner._eq[-1]
                    sb.tasks.setdefault(tid, {}).update(row)
                sb.recorder.append(("update", "automation_tasks", row,
                                    list(self_inner._eq)))
                return self_inner
        return _Upd()


def _make_phase(idx, enabled=True):
    return pr.PhaseSpec(
        id=f"p{idx}",
        position=idx,
        name=f"Phase {idx}",
        model_id="anthropic:claude-sonnet-4-6",
        system_prompt="prompt",
        enabled=enabled,
    )


def _make_req(phases, **kw):
    return pr.RunPipelineRequest(
        chat_id="chat-test",
        project_id="proj-test",
        agent_chat_url="http://x/api/chat",
        supabase_url="http://supa",
        supabase_service_key="k",
        phases=phases,
        **kw,
    )


def _inserts_of(sb, msg_type):
    out = []
    for entry in sb.recorder:
        # Insert entries look like: (("insert", table_name), payload_dict)
        if not isinstance(entry, tuple) or len(entry) != 2:
            continue
        op_key, payload = entry
        if not (isinstance(op_key, tuple) and op_key and op_key[0] == "insert"):
            continue
        if isinstance(payload, dict) and payload.get("type") == msg_type:
            out.append(payload)
    return out


def _task_updates(sb):
    """All automation_tasks UPDATE payloads recorded by the fake client."""
    out = []
    for entry in sb.recorder:
        if (isinstance(entry, tuple) and len(entry) == 4
                and entry[0] == "update" and entry[1] == "automation_tasks"):
            out.append(entry[2])
    return out


def _run(req, sb, stub_run_phase=None):
    """Run _run_pipeline with mocked supabase + stubbed _run_phase."""
    orig_client = pr._sb_client
    orig_run_phase = pr._run_phase
    pr._sb_client = lambda r: sb

    async def default_phase(sb, r, phase, i, total, prior):
        return f"output of phase {i}"
    pr._run_phase = stub_run_phase or default_phase
    try:
        asyncio.run(pr._run_pipeline(req))
    finally:
        pr._sb_client = orig_client
        pr._run_phase = orig_run_phase
        # Release in-memory active-lock so successive tests don't collide.
        pr._active_pipelines.discard(req.chat_id)


def test_full_run_no_alarm():
    sb = _FakeSupabase()
    req = _make_req([_make_phase(i) for i in (1, 2, 3)], task_id="t-full")
    _run(req, sb)

    finals = _inserts_of(sb, "final")
    errors = _inserts_of(sb, "error")
    statuses = _inserts_of(sb, "status")

    assert any(f["metadata"].get("kind") == "pipeline_complete" for f in finals), \
        "missing terminal final"
    assert not errors, f"unexpected alarm fired: {errors}"
    assert any(s["metadata"].get("kind") == "pipeline_complete" for s in statuses)


def test_cooperative_stop_emits_final():
    sb = _FakeSupabase()
    sb.tasks["t1"] = {"stop_requested": True, "status": "running"}
    req = _make_req([_make_phase(i) for i in (1, 2, 3)], task_id="t1")
    _run(req, sb)

    finals = _inserts_of(sb, "final")
    assert any(f["metadata"].get("kind") == "pipeline_stopped" for f in finals), \
        "cooperative stop should emit a terminal `final` row"
    # The finally safety net must NOT clobber a cooperatively-stopped task: the
    # status flips to 'stopped' and is never re-flipped to 'failed'.
    updates = _task_updates(sb)
    assert any(u.get("status") == "stopped" for u in updates), \
        f"cooperative stop did not flip task to stopped: {updates}"
    assert not any(u.get("status") == "failed" for u in updates), \
        f"safety net wrongly re-flipped a stopped task to failed: {updates}"


def test_cancellation_flips_task_to_failed():
    """If the run is cancelled mid-phase (CancelledError — graceful shutdown /
    restart), no try/except branch flips the task, so the `finally` safety net
    must mark it 'failed'. Without the net the row would stick on 'running'."""
    sb = _FakeSupabase()
    req = _make_req([_make_phase(1)], task_id="t-cancel")

    async def _cancel_phase(sb, r, phase, i, total, prior):
        raise asyncio.CancelledError()

    try:
        _run(req, sb, stub_run_phase=_cancel_phase)
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("expected CancelledError to propagate")

    updates = _task_updates(sb)
    failed = [u for u in updates if u.get("status") == "failed"]
    assert failed, f"cancelled run not flipped to failed by safety net: {updates}"
    assert "error" in failed[0], "must write the real `error` column"


def test_disabled_phases_trigger_alarm():
    sb = _FakeSupabase()
    # Caller submitted 4 phases but only enabled 1 — same shape as chat
    # 88f73936 if the caller had at least *declared* the other phases.
    phases = [_make_phase(1, enabled=True)] + \
             [_make_phase(i, enabled=False) for i in (2, 3, 4)]
    req = _make_req(phases, task_id="t-disabled")
    _run(req, sb)

    # Task #49: the phase-skip advisory is a non-terminal `status` row (kind
    # preserved), NOT an `error` row — an error row would be a second terminal
    # alongside the success `final` and double up the turn.
    statuses = _inserts_of(sb, "status")
    alarms = [s for s in statuses
              if s["metadata"].get("kind") == "pipeline_phases_skipped"]
    assert alarms, f"phase-skip alarm did not fire: statuses={statuses}"
    assert alarms[0]["metadata"]["reason"] == "disabled-by-caller"
    assert set(alarms[0]["metadata"]["skipped_phases"]) == {
        "Phase 2", "Phase 3", "Phase 4"
    }
    # Successful final row still written.
    finals = _inserts_of(sb, "final")
    assert any(f["metadata"].get("kind") == "pipeline_complete" for f in finals)


def test_expected_min_violation_rejects_up_front():
    sb = _FakeSupabase()
    # Caller declares ABM (6 phases) but only enables 1.
    req = _make_req(
        [_make_phase(1)],
        expected_phases_min=6,
        pipeline_shape="ABM",
        task_id="t-misconfig",
    )
    phase_ran = {"count": 0}

    async def counting_phase(sb, r, phase, i, total, prior):
        phase_ran["count"] += 1
        return "x"
    _run(req, sb, stub_run_phase=counting_phase)

    assert phase_ran["count"] == 0, \
        "runner should refuse to start truncated ABM run"
    errors = _inserts_of(sb, "error")
    rej = [e for e in errors
           if e["metadata"].get("kind") == "pipeline_misconfigured"]
    assert rej, f"early rejection did not fire: errors={errors}"
    assert rej[0]["metadata"]["expected_min"] == 6
    assert rej[0]["metadata"]["enabled_count"] == 1
    # Task #49: the misconfigured branch emits ONLY a terminal `error` row
    # (the UI treats `error` as turn-ending) — never a redundant `final`.
    finals = _inserts_of(sb, "final")
    assert not any(f["metadata"].get("kind") == "pipeline_misconfigured"
                   for f in finals), \
        "misconfigured path must not emit a redundant `final` row"
    # The automation_tasks row must be flipped to `failed` (with the real
    # `error` column) so the automations UI never sticks on `running`.
    updates = _task_updates(sb)
    failed = [u for u in updates if u.get("status") == "failed"]
    assert failed, f"misconfigured task was not flipped to failed: {updates}"
    assert "error" in failed[0]
    assert all("last_error" not in u for u in updates), \
        "must never write the non-existent last_error column"


if __name__ == "__main__":
    test_full_run_no_alarm()
    test_cooperative_stop_emits_final()
    test_cancellation_flips_task_to_failed()
    test_disabled_phases_trigger_alarm()
    test_expected_min_violation_rejects_up_front()
    print("OK — task #32 regression guards green.")
