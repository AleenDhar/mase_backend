---
name: Analysis run recovery after interruption
description: Why interrupted analysis Run-All leaves status frozen, and how to recover it.
---

# Interrupted analysis runs leave `analyses.status="running"` frozen

The Run-All concurrency guard (`analysis_engine._active_tasks` / `_stop_events`) is
**process-local, in-memory only**. A server restart (the "DeepAgent Server" workflow
has NO auto-reload, so any code change forces a restart) kills the in-flight Run-All
task but never resets the DB: `analyses.status` stays `"running"` and the run record
stays open, so the UI shows a frozen "computing… N/total" with no live task behind it.

**Why:** the guard was designed for single-process mutual exclusion, not durable run
state. `is_running()` reads memory, so after a restart it correctly returns False, but
nothing reconciles the orphaned DB status.

**How to recover (do NOT re-run all cells):** call `POST /api/analysis/{id}/resume`
(`engine.start_resume` → `_run_analysis(..., resume=True)`). It keeps already-`done`
AI cells (still feeding their values to right-hand columns) and recomputes only the
remaining pending/running/error cells with normal row-parallelism, then resets
`analyses.status` to `done`/`error` when finished. Same per-analysis guard as
`start_run` (`409` if a run/rerun is active).

**Operational notes:**
- The resume runs server-side as a tracked background task — it survives the agent's
  bash calls. Background processes launched from the agent's own bash tool (even with
  `nohup`) get killed when the tool call's shell exits, so drive long work through the
  running server (REST), not standalone scripts.
- Re-running cells via the in-server endpoint is required for correct results: the tool
  provider (`set_tool_provider`) is only registered inside the running server, so a
  standalone script would compute cells without tools.
- Tool-using cells can take minutes each (model timeout 180s × retries + tool loops);
  a 288-cell analysis can take ~20+ min to fully resume at 4-way row concurrency.
