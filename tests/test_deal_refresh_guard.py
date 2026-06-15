"""Deterministic tests for the deal-refresh mutual-exclusion safety guard.

The token-free hard-fact refresh (`hard_refresh_all`) and the AI deal sweep both
rewrite the FULL deal record, so they must never run concurrently — one would
clobber the other's work. The guard is a process-local flag pair plus a check of
the durable cross-process sweep queue:

  * hard refresh ABORTS (returns a {"skipped": ...} marker, does no work) while an
    AI sweep is running in-process (`_RUN_STATE["status"]=="running"`), while sweep
    rows are waiting/working in the durable queue, and while another hard refresh
    is already running.
  * every sweep enqueue path (book run, single-opp trigger, scheduled discovery)
    REFUSES to add work while a hard refresh has its guard set.

These tests exercise that guard with NO live network / Salesforce: the dependency
seams (`_queue`, `store`) are stubbed and the early refusal returns happen before
any I/O, so the guards are proven in isolation.

Run: python3 -m pytest tests/test_deal_refresh_guard.py -q
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deal_engine_sweep as S  # noqa: E402


class _GuardState:
    """Snapshot + restore every module global / seam these tests poke, so a
    failing assert can never leak a stuck guard into the next test."""

    def __enter__(self):
        self._run_state = dict(S._RUN_STATE)
        self._hard = S._hard_refresh_running
        self._disc = S._discovery_running
        self._queue = S._queue
        self._store_is_active = getattr(S.store, "is_active_member", None)
        self._store_list_records = getattr(S.store, "list_records", None)
        # Neuter the audit-log write by default so a test never touches Supabase;
        # capture tests reassign S._hard_refresh_log.log_run themselves.
        self._log_run = S._hard_refresh_log.log_run
        S._hard_refresh_log.log_run = lambda *_a, **_k: None
        return self

    def __exit__(self, *exc):
        S._RUN_STATE.clear()
        S._RUN_STATE.update(self._run_state)
        S._hard_refresh_running = self._hard
        S._discovery_running = self._disc
        S._queue = self._queue
        if self._store_is_active is not None:
            S.store.is_active_member = self._store_is_active
        if self._store_list_records is not None:
            S.store.list_records = self._store_list_records
        S._hard_refresh_log.log_run = self._log_run
        return False


class _FakeQueue:
    """Stand-in for the sweep_queue module exposing only what the guards read."""

    def __init__(self, *, waiting=0, working=0):
        self._snap = {"waiting": waiting, "working": working}

    def status(self):
        return dict(self._snap)


def _idle_run_state():
    S._RUN_STATE.clear()
    S._RUN_STATE.update({"status": "idle"})


# ---- hard refresh ABORTS while a sweep is active ----------------------------

def test_hard_refresh_aborts_when_already_running():
    """A second hard refresh while one holds the guard is a no-op (serialized)."""
    with _GuardState():
        S._hard_refresh_running = True
        out = asyncio.run(S.hard_refresh_all(agent_manager=None))
        assert out["skipped"] == "hard_refresh_in_progress"
        assert out["status"] == "skipped"
        # The early return must NOT clear the guard the other refresh still holds.
        assert S._hard_refresh_running is True


def test_hard_refresh_aborts_when_inprocess_sweep_running():
    """Legacy in-process AI sweep (`_RUN_STATE`) blocks the hard refresh."""
    with _GuardState():
        S._hard_refresh_running = False
        _idle_run_state()
        S._RUN_STATE["status"] = "running"
        S._queue = _FakeQueue(waiting=0, working=0)
        out = asyncio.run(S.hard_refresh_all(agent_manager=None))
        assert out["skipped"] == "sweep_in_progress"
        assert out["status"] == "skipped"
        # Self-guard released on the way out so a later refresh isn't wedged.
        assert S._hard_refresh_running is False


def test_hard_refresh_aborts_when_queue_has_waiting_rows():
    """Durable cross-process queue work (waiting) blocks the hard refresh."""
    with _GuardState():
        S._hard_refresh_running = False
        _idle_run_state()
        S._queue = _FakeQueue(waiting=3, working=0)
        out = asyncio.run(S.hard_refresh_all(agent_manager=None))
        assert out["skipped"] == "sweep_queue_active"
        assert out["waiting"] == 3 and out["working"] == 0
        assert S._hard_refresh_running is False


def test_hard_refresh_aborts_when_queue_has_working_rows():
    """A row a worker is actively processing (working) also blocks it."""
    with _GuardState():
        S._hard_refresh_running = False
        _idle_run_state()
        S._queue = _FakeQueue(waiting=0, working=1)
        out = asyncio.run(S.hard_refresh_all(agent_manager=None))
        assert out["skipped"] == "sweep_queue_active"
        assert out["working"] == 1
        assert S._hard_refresh_running is False


# ---- enqueue paths REFUSE while a hard refresh is running -------------------

def test_book_run_enqueue_refused_during_hard_refresh():
    """enqueue_book_run raises (no work queued) while the hard refresh runs."""
    with _GuardState():
        _idle_run_state()
        S._discovery_running = False
        S._hard_refresh_running = True
        # Empty queue so we get PAST the "sweep already in progress" check and
        # reach the hard-refresh guard specifically.
        S._queue = _FakeQueue(waiting=0, working=0)
        try:
            asyncio.run(S.enqueue_book_run(agent_manager=None))
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "hard refresh" in str(e).lower()


def test_trigger_enqueue_refused_during_hard_refresh():
    """enqueue_trigger returns 'skipped_hard_refresh' (no row enqueued)."""
    with _GuardState():
        _idle_run_state()
        S._hard_refresh_running = True
        # The opp must look like a book member so we reach the hard-refresh guard
        # (membership is checked first) — stub that read, never the network.
        S.store.is_active_member = lambda *_a, **_k: True
        out = asyncio.run(S.enqueue_trigger(agent_manager=None, opp_id="0065g00000ABCDEFGHI"))
        assert out == "skipped_hard_refresh"


def test_discovery_enqueue_refused_during_hard_refresh():
    """Scheduled discovery skips (no analyze/upsert) while the hard refresh runs."""
    with _GuardState():
        _idle_run_state()
        S._discovery_running = False
        S._hard_refresh_running = True
        out = asyncio.run(S.discover_and_sweep_new(agent_manager=None))
        assert out["skipped"] == "hard_refresh_in_progress"
        assert out["swept"] == 0 and out["new"] == 0
        # Guard released, discovery not left flagged as running.
        assert S._discovery_running is False


# ---- EVERY invocation appends exactly one audit-history row -----------------

def test_hard_refresh_logs_skip_row():
    """A skipped run (another refresh already holds the guard) still appends one
    history row marked status='skipped' — the nightly cadence stays auditable."""
    with _GuardState():
        S._hard_refresh_running = True
        logged: list = []
        S._hard_refresh_log.log_run = logged.append
        out = asyncio.run(S.hard_refresh_all(agent_manager=None, source="nightly_cron"))
        assert out["skipped"] == "hard_refresh_in_progress"
        assert len(logged) == 1
        row = logged[0]
        assert row["status"] == "skipped"
        assert row["source"] == "nightly_cron"
        assert row.get("finished_at")


def test_hard_refresh_logs_failure_row_and_reraises():
    """A fatal failure mid-run appends one history row marked status='failed'
    (with the error captured) and the exception is re-raised to the caller."""
    with _GuardState():
        _idle_run_state()
        S._hard_refresh_running = False
        S._queue = _FakeQueue(waiting=0, working=0)
        logged: list = []
        S._hard_refresh_log.log_run = logged.append

        def _boom(*_a, **_k):
            raise RuntimeError("kaboom")

        S.store.list_records = _boom
        try:
            asyncio.run(S.hard_refresh_all(agent_manager=None, source="manual"))
            assert False, "expected RuntimeError to propagate"
        except RuntimeError as e:
            assert "kaboom" in str(e)
        assert len(logged) == 1
        row = logged[0]
        assert row["status"] == "failed"
        assert "kaboom" in (row.get("error") or "")
        assert row.get("finished_at")
        # Guard released even on the failure path.
        assert S._hard_refresh_running is False
