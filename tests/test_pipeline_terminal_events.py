"""Regression guard for task #31 (terminal row emitted) + task #49 (exactly
one terminal row per run).

The UI only stops its "Thinking…" spinner on a chat row whose type is
`final` (or `error`). A `status` row with kind=pipeline_complete is *not*
enough — that was the silent-hang bug in chat 88f73936 (task #31).

Task #49 then tightened the contract: every run must emit EXACTLY ONE
terminal row. The UI treats both `final` and `error` as terminal, so the old
belt-and-braces pattern (write `error` then also `final`) produced a
duplicate terminal. The cure is the `_emit_terminal` guard + a `finally`
safety net; reliability now comes from the `_insert_row` retry layer, not
from writing a second terminal row.

To keep the cure from regressing without spinning up Supabase + mock phases,
we scan the `_run_pipeline` source for the terminal sites and assert each one
still emits a terminating row, and that the single-terminal guard is present.
If anyone refactors and drops the emit (or reintroduces a dual write), this
test fails loudly during CI / local pytest runs.
"""
from __future__ import annotations

import ast
import pathlib


def _load_run_pipeline_source() -> str:
    src = pathlib.Path(__file__).resolve().parents[1] / "pipeline_runner.py"
    tree = ast.parse(src.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_pipeline":
            return ast.unparse(node)
    raise AssertionError("_run_pipeline not found in pipeline_runner.py")


def test_pipeline_complete_emits_final_row() -> None:
    body = _load_run_pipeline_source()
    assert "pipeline_complete" in body, "pipeline_complete marker missing"
    # The pipeline_complete branch must write at least one row whose type
    # column is the literal 'final'. We look for the _insert_row call shape.
    assert "'final'" in body or '"final"' in body, (
        "_run_pipeline no longer writes a type='final' row — UI will hang "
        "on successful runs. Task #31 regression."
    )


def test_pipeline_stopped_branch_emits_final_row() -> None:
    body = _load_run_pipeline_source()
    assert "pipeline_stopped" in body, "pipeline_stopped marker missing"
    # The stopped branch sits inside the for-loop. We require that the
    # surrounding text mentions both pipeline_stopped and 'final' so a
    # future refactor that drops the final emit fails this test.
    stopped_idx = body.find("pipeline_stopped")
    # Look within 800 chars after the first pipeline_stopped occurrence —
    # the final-row insert should be in that window.
    window = body[stopped_idx:stopped_idx + 1200]
    assert "'final'" in window or '"final"' in window, (
        "pipeline_stopped branch no longer writes a type='final' row — UI "
        "will hang when the user stops a run. Task #31 regression."
    )


def test_pipeline_error_branch_emits_terminal_row() -> None:
    body = _load_run_pipeline_source()
    assert "pipeline_error" in body, "pipeline_error marker missing"
    # The outer-exception (driver error) branch must emit a single terminal
    # `error` row. The UI treats `error` as terminal; reliability comes from
    # the _insert_row retry layer + the finally safety net, NOT from a second
    # belt-and-braces `final` row (task #49 collapsed that dual write).
    err_idx = body.find("pipeline_error")
    window = body[err_idx:err_idx + 2000]
    assert "'error'" in window or '"error"' in window, (
        "pipeline_error branch no longer writes type='error'. Task #31 "
        "regression."
    )


def test_run_pipeline_has_single_terminal_guard() -> None:
    """Task #49: the run must funnel terminals through `_emit_terminal` and a
    `finally` safety net so exactly one terminal row is written per run. Guard
    against a refactor that drops the guard or reintroduces a dual error+final
    write in a single branch."""
    body = _load_run_pipeline_source()
    assert "_emit_terminal" in body, (
        "_emit_terminal guard missing — terminal rows are no longer funnelled "
        "through the single-terminal writer. Task #49 regression."
    )
    assert "terminal_written" in body, (
        "terminal_written flag / finally safety net missing. Task #49 "
        "regression."
    )
    # No branch should emit both a 'final' and an 'error' terminal back to
    # back (the old belt-and-braces pattern). We approximate this by asserting
    # the driver-error window does not also write a 'final' row.
    err_idx = body.find("pipeline_error")
    window = body[err_idx:err_idx + 2000]
    assert "'final'" not in window and '"final"' not in window, (
        "driver-error branch writes both 'error' and 'final' — the dual "
        "terminal write was reintroduced. Task #49 regression."
    )


if __name__ == "__main__":
    test_pipeline_complete_emits_final_row()
    test_pipeline_stopped_branch_emits_final_row()
    test_pipeline_error_branch_emits_terminal_row()
    test_run_pipeline_has_single_terminal_guard()
    print("OK — terminal branches emit exactly one UI-terminating row.")
