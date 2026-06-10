"""analysis_engine.py — the Run-All engine for AI columns (Task #52).

Fills every AI cell of an analysis by calling the column's configured LLM with
its own system prompt over the row's data. Rows run with bounded parallelism;
within a row the AI columns run SEQUENTIALLY left-to-right so a later column can
consume an earlier column's output.

Run lifecycle (start / stop / is_running) is tracked here in-process via an
asyncio registry keyed by analysis_id, giving a per-analysis concurrency guard
(exactly one active run per analysis). All callers (REST endpoints, agent tools)
run inside the server event loop.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

import analysis_store as store

_ROW_CONCURRENCY = max(1, int(os.getenv("ANALYSIS_ROW_CONCURRENCY", "4") or "4"))
_CELL_MAX_CHARS = int(os.getenv("ANALYSIS_CELL_MAX_CHARS", "8000") or "8000")
_CTX_FIELD_MAX = 1200
# Recursion budget for a tool-using AI cell (each model<->tool round trip counts).
# Bounds runaway tool loops while still allowing a few lookups per cell.
_CELL_TOOL_RECURSION = max(2, int(os.getenv("ANALYSIS_CELL_TOOL_RECURSION", "12") or "12"))

# Tool provider — set by the server at startup so each AI cell can use the SAME
# catalog (custom + MCP tools, denylist already applied) as the main chat agent.
# Kept as a getter so it reflects the live catalog as MCP servers (re)load.
_tool_provider = None


def set_tool_provider(fn) -> None:
    """Register a no-arg callable returning the shared tool list. Called once by
    the server after the agent manager is built."""
    global _tool_provider
    _tool_provider = fn


def _get_tools() -> list:
    if _tool_provider is None:
        return []
    try:
        return list(_tool_provider() or [])
    except Exception as e:  # noqa: BLE001
        print(f"[ANALYSIS] tool provider failed: {e}")
        return []


_DEFAULT_MODEL = os.getenv("ANALYSIS_DEFAULT_MODEL") or os.getenv("MODEL", "anthropic:claude-sonnet-4-6-20260901")

SUPPORTED_PROVIDERS = {"anthropic", "openai", "google", "google_genai", "xai"}

# Curated suggestions surfaced to the frontend (the backend still accepts any
# "provider:model" whose provider is supported).
MODEL_SUGGESTIONS = [
    {"id": _DEFAULT_MODEL, "label": "Claude Sonnet (default)", "provider": "anthropic"},
    {"id": "anthropic:claude-3-5-haiku-latest", "label": "Claude 3.5 Haiku (fast)", "provider": "anthropic"},
    {"id": "openai:gpt-4o", "label": "GPT-4o", "provider": "openai"},
    {"id": "openai:gpt-4o-mini", "label": "GPT-4o mini (fast)", "provider": "openai"},
    {"id": "google:gemini-1.5-pro", "label": "Gemini 1.5 Pro", "provider": "google"},
    {"id": "google:gemini-1.5-flash", "label": "Gemini 1.5 Flash (fast)", "provider": "google"},
]


class AnalysisRunError(Exception):
    """Concurrency / state conflict (maps to HTTP 409)."""
    pass


class AnalysisNotFound(Exception):
    """Target analysis/row/column/cell does not exist (maps to HTTP 404)."""
    pass


def validate_model(model_str: Optional[str]) -> str:
    """Normalise + validate a 'provider:model' string. Defaults provider to
    anthropic when no prefix is given. Raises on unsupported provider."""
    s = (model_str or _DEFAULT_MODEL).strip()
    provider = "anthropic"
    name = s
    if ":" in s:
        provider, name = s.split(":", 1)
        provider = provider.strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise AnalysisRunError(
            f"Unsupported model provider '{provider}'. Use one of {sorted(SUPPORTED_PROVIDERS)} "
            f"as 'provider:model' (e.g. 'openai:gpt-4o')."
        )
    return f"{provider}:{name.strip()}"


def resolve_model(model_str: Optional[str]):
    full = validate_model(model_str)
    provider, name = full.split(":", 1)
    if provider == "xai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=name, api_key=os.environ.get("XAI_API_KEY"),
                          base_url="https://api.x.ai/v1", temperature=0)
    if provider == "google":
        provider = "google_genai"
    from langchain.chat_models import init_chat_model
    return init_chat_model(name, model_provider=provider, temperature=0)


def _short(v: Any, max_chars: int = _CTX_FIELD_MAX) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
    if len(s) > max_chars:
        s = s[:max_chars] + f"… [+{len(s) - max_chars} chars]"
    return s


def _build_context(col: dict, row: dict, computed: dict, cols_by_id: dict) -> str:
    cfg = col.get("config") or {}
    parts = []
    label = row.get("label") or row.get("entity_ref") or "(row)"
    parts.append(f"Row: {label}")
    if row.get("entity_ref"):
        parts.append(f"Entity ref: {row['entity_ref']}")

    src = row.get("source") or {}
    fields = cfg.get("input_fields") or []
    if fields:
        for f in fields:
            if f in src:
                parts.append(f"{f}: {_short(src[f])}")
    inp = cfg.get("input_columns")
    if inp:
        for cid in inp:
            if cid in computed:
                c = cols_by_id.get(cid)
                parts.append(f"{(c['name'] if c else cid)}: {_short(computed[cid])}")
    elif not fields:
        # No explicit inputs configured → expose everything computed so far for
        # this row (all data columns + earlier AI columns) plus the raw source.
        for cid, val in computed.items():
            if cid == col["id"]:
                continue
            c = cols_by_id.get(cid)
            parts.append(f"{(c['name'] if c else cid)}: {_short(val)}")
        if src:
            parts.append("Source data: " + _short(src, 2000))

    instr = cfg.get("instructions")
    if instr:
        parts.append(f"\nTask: {instr}")
    return "\n".join(parts)


def _msg_text(msg) -> str:
    c = getattr(msg, "content", "")
    return c if isinstance(c, str) else json.dumps(c, default=str)


def _sum_tokens(messages: list) -> Optional[int]:
    total = 0
    seen = False
    for m in messages:
        um = getattr(m, "usage_metadata", None)
        if isinstance(um, dict) and um.get("total_tokens"):
            total += um["total_tokens"]
            seen = True
    return total if seen else None


async def _run_ai_cell(col: dict, row: dict, computed: dict, cols_by_id: dict):
    from langchain_core.messages import SystemMessage, HumanMessage
    cfg = col.get("config") or {}
    system_prompt = (cfg.get("system_prompt") or
                     "You are a precise sales analyst. Answer concisely. Use the tools "
                     "available to look up any missing facts; if the data is still "
                     "insufficient, say so plainly.")
    model_str = cfg.get("model") or _DEFAULT_MODEL
    llm = resolve_model(model_str)
    context = _build_context(col, row, computed, cols_by_id)

    # An AI column may use the shared tool catalog (same tools as the main chat
    # agent, denylist already applied). Default on; set config.use_tools=false to
    # force a plain single-shot completion for cheaper/faster columns.
    use_tools = cfg.get("use_tools", True)
    tools = _get_tools() if use_tools else []
    if tools:
        try:
            from langgraph.prebuilt import create_react_agent
            agent = create_react_agent(llm, tools, prompt=system_prompt)
            result = await agent.ainvoke(
                {"messages": [HumanMessage(content=context)]},
                config={"recursion_limit": _CELL_TOOL_RECURSION},
            )
            msgs = result.get("messages", []) if isinstance(result, dict) else []
            content = _msg_text(msgs[-1]) if msgs else ""
            value = content[:_CELL_MAX_CHARS]
            return value, model_str, _sum_tokens(msgs)
        except Exception as e:  # noqa: BLE001
            # Never fail a cell just because the tool loop errored (e.g. recursion
            # limit hit) — fall back to a plain completion so the run continues.
            print(f"[ANALYSIS] tool-agent cell fell back to plain completion: {e}")

    ai = await llm.ainvoke([SystemMessage(content=system_prompt), HumanMessage(content=context)])
    content = ai.content if isinstance(ai.content, str) else json.dumps(ai.content, default=str)
    value = content[:_CELL_MAX_CHARS]
    tokens = None
    um = getattr(ai, "usage_metadata", None)
    if isinstance(um, dict):
        tokens = um.get("total_tokens")
    return value, model_str, tokens


async def _run_analysis(analysis_id: str, stop_event: asyncio.Event,
                        resume: bool = False) -> dict:
    """Fill AI cells of an analysis.

    When ``resume`` is True, cells already marked ``done`` are left untouched
    (their value is still fed to columns to their right) and only the remaining
    pending/running/error cells are recomputed. This is used to finish a run
    that was interrupted (e.g. a server restart) without redoing completed work.
    """
    columns = await asyncio.to_thread(store.list_columns, analysis_id)
    rows = await asyncio.to_thread(store.list_rows, analysis_id)
    cols_by_id = {c["id"]: c for c in columns}
    ai_cols = sorted([c for c in columns if c.get("type") == "ai"], key=lambda c: c.get("position", 0))
    data_cols = [c for c in columns if c.get("type") == "data"]

    # Make sure data cells reflect the latest source values before AI runs.
    await asyncio.to_thread(store.populate_data_cells, analysis_id)

    total = len(ai_cols) * len(rows)
    run_id = None

    # On resume, pre-load existing AI cell state so done cells are skipped and
    # their values are still available to dependent (right-hand) columns.
    existing: dict = {}
    already_done = 0
    if resume:
        for c in await asyncio.to_thread(store.list_cells, analysis_id):
            if c.get("column_id") in cols_by_id and cols_by_id[c["column_id"]].get("type") == "ai":
                existing[(c["row_id"], c["column_id"])] = c
                if c.get("status") == "done":
                    already_done += 1

    counters = {"done": already_done, "error": 0}
    lock = asyncio.Lock()
    sem = asyncio.Semaphore(_ROW_CONCURRENCY)

    async def process_row(row: dict):
        async with sem:
            computed: dict = {}
            for c in data_cols:
                computed[c["id"]] = store._data_value(c, row)
            for col in ai_cols:
                if stop_event.is_set():
                    return
                if resume:
                    ex = existing.get((row["id"], col["id"]))
                    if ex and ex.get("status") == "done" and ex.get("value") is not None:
                        # Already computed — keep it, feed it to later columns.
                        computed[col["id"]] = ex["value"]
                        continue
                await asyncio.to_thread(store.upsert_cell, analysis_id, row["id"], col["id"],
                                        status="running", error="")
                try:
                    value, model_used, tokens = await _run_ai_cell(col, row, computed, cols_by_id)
                    computed[col["id"]] = value
                    await asyncio.to_thread(store.upsert_cell, analysis_id, row["id"], col["id"],
                                            value=value, status="done", error="",
                                            model_used=model_used, tokens_used=tokens)
                    async with lock:
                        counters["done"] += 1
                        await asyncio.to_thread(store.update_run, run_id, {"cells_done": counters["done"]})
                except Exception as e:  # noqa: BLE001
                    await asyncio.to_thread(store.upsert_cell, analysis_id, row["id"], col["id"],
                                            status="error", error=str(e)[:1000])
                    async with lock:
                        counters["error"] += 1
                        await asyncio.to_thread(store.update_run, run_id, {"cells_error": counters["error"]})

    try:
        run = await asyncio.to_thread(store.create_run, analysis_id, total)
        run_id = run["id"]
        await asyncio.to_thread(store.update_analysis, analysis_id, {"status": "running"})
        if resume and already_done:
            # Reflect work already completed so the UI progress resumes at N/total.
            await asyncio.to_thread(store.update_run, run_id, {"cells_done": already_done})
        if ai_cols and rows:
            if resume:
                # Only mark the cells we will actually (re)compute as pending, so
                # done cells keep their value and the UI shows them as complete.
                pairs = [(r["id"], c["id"]) for r in rows for c in ai_cols
                         if not (existing.get((r["id"], c["id"])) or {}).get("status") == "done"]
            else:
                # Mark every AI cell pending up-front so the frontend shows the
                # queued state (contract: pending -> running -> done/error).
                pairs = [(r["id"], c["id"]) for r in rows for c in ai_cols]
            if pairs:
                await asyncio.to_thread(store.set_cells_status, analysis_id, pairs, "pending")
            await asyncio.gather(*(process_row(r) for r in rows))
        stopped = stop_event.is_set()
        final_status = "stopped" if stopped else ("error" if counters["error"] else "done")
        await asyncio.to_thread(store.update_run, run_id, {
            "status": final_status,
            "cells_done": counters["done"],
            "cells_error": counters["error"],
            "finished_at": store._now(),
        })
        # Keep analyses.status consistent with the run outcome.
        analysis_status = ("draft" if stopped
                           else ("error" if counters["error"] else "done"))
        await asyncio.to_thread(store.update_analysis, analysis_id, {"status": analysis_status})
        return {"run_id": run_id, "status": final_status, **counters, "total": total}
    except Exception as e:  # noqa: BLE001
        if run_id:
            await asyncio.to_thread(store.update_run, run_id, {
                "status": "error", "error": str(e)[:1000], "finished_at": store._now(),
            })
        await asyncio.to_thread(store.update_analysis, analysis_id, {"status": "error"})
        raise


# ---------- run registry (per-analysis concurrency guard) ----------

_active_tasks: dict[str, asyncio.Task] = {}
_stop_events: dict[str, asyncio.Event] = {}
# analysis_ids with an in-flight single-cell re-run. Mutual exclusion vs Run-All
# is enforced by claiming this set synchronously (no `await` between check and
# claim), which is atomic under asyncio's single-threaded cooperative scheduling.
_rerun_active: set[str] = set()


def is_running(analysis_id: str) -> bool:
    t = _active_tasks.get(analysis_id)
    return bool(t and not t.done())


def start_run(analysis_id: str) -> dict:
    """Launch a Run-All in the background. Raises if a run OR a single-cell
    re-run is already active for this analysis. Must be called from within the
    server event loop. The check-and-claim below contains no `await`, so it is
    atomic against a concurrent rerun_cell()."""
    if is_running(analysis_id):
        raise AnalysisRunError("A run is already in progress for this analysis.")
    if analysis_id in _rerun_active:
        raise AnalysisRunError("A single-cell re-run is in progress for this analysis.")
    stop_event = asyncio.Event()
    _stop_events[analysis_id] = stop_event

    async def _runner():
        try:
            return await _run_analysis(analysis_id, stop_event)
        finally:
            _stop_events.pop(analysis_id, None)
            _active_tasks.pop(analysis_id, None)

    task = asyncio.create_task(_runner())
    _active_tasks[analysis_id] = task
    return {"status": "started", "analysis_id": analysis_id}


def start_resume(analysis_id: str) -> dict:
    """Launch a Run-All in RESUME mode: only the non-done AI cells are
    (re)computed; already-done cells keep their values. Used to finish a run
    that was interrupted (e.g. a server restart left the analysis frozen).
    Same per-analysis concurrency guard as start_run()."""
    if is_running(analysis_id):
        raise AnalysisRunError("A run is already in progress for this analysis.")
    if analysis_id in _rerun_active:
        raise AnalysisRunError("A single-cell re-run is in progress for this analysis.")
    stop_event = asyncio.Event()
    _stop_events[analysis_id] = stop_event

    async def _runner():
        try:
            return await _run_analysis(analysis_id, stop_event, resume=True)
        finally:
            _stop_events.pop(analysis_id, None)
            _active_tasks.pop(analysis_id, None)

    task = asyncio.create_task(_runner())
    _active_tasks[analysis_id] = task
    return {"status": "resuming", "analysis_id": analysis_id}


def stop_run(analysis_id: str) -> dict:
    ev = _stop_events.get(analysis_id)
    if ev and not ev.is_set():
        ev.set()
        return {"status": "stopping", "analysis_id": analysis_id}
    return {"status": "not_running", "analysis_id": analysis_id}


# ---------- single-cell re-run ----------

async def rerun_cell(analysis_id: str, *, cell_id: Optional[str] = None,
                     row_id: Optional[str] = None, column_id: Optional[str] = None) -> dict:
    """Re-run ONE AI cell. Resolve by cell_id, or by (row_id, column_id).
    Earlier AI columns + data columns for that row are used as inputs."""
    # Atomic check-and-claim: no `await` between these checks and adding to
    # _rerun_active, so this is mutually exclusive with start_run() and with a
    # second concurrent rerun_cell() under asyncio's cooperative scheduling.
    if is_running(analysis_id):
        raise AnalysisRunError("A full run is in progress; stop it before re-running a cell.")
    if analysis_id in _rerun_active:
        raise AnalysisRunError("Another single-cell re-run is in progress for this analysis.")
    _rerun_active.add(analysis_id)
    try:
        if cell_id:
            cell = await asyncio.to_thread(store.get_cell, cell_id)
            if not cell:
                raise AnalysisNotFound(f"No cell with id '{cell_id}'.")
            row_id, column_id = cell["row_id"], cell["column_id"]
        if not (row_id and column_id):
            raise ValueError("Provide cell_id or both row_id and column_id.")

        columns = await asyncio.to_thread(store.list_columns, analysis_id)
        cols_by_id = {c["id"]: c for c in columns}
        col = cols_by_id.get(column_id)
        if not col:
            raise AnalysisNotFound(f"No column with id '{column_id}'.")
        if col.get("type") != "ai":
            raise ValueError("Only AI cells can be re-run.")

        rows = await asyncio.to_thread(store.list_rows, analysis_id)
        row = next((r for r in rows if r["id"] == row_id), None)
        if not row:
            raise AnalysisNotFound(f"No row with id '{row_id}'.")

        data_cols = [c for c in columns if c.get("type") == "data"]
        ai_cols = sorted([c for c in columns if c.get("type") == "ai"],
                         key=lambda c: c.get("position", 0))
        computed: dict = {}
        for c in data_cols:
            computed[c["id"]] = store._data_value(c, row)
        for c in ai_cols:
            if c["id"] == column_id:
                break  # only columns to the LEFT feed this one
            ex = await asyncio.to_thread(store.get_cell_by_pair, analysis_id, row_id, c["id"])
            if ex and ex.get("value") is not None:
                computed[c["id"]] = ex["value"]

        await asyncio.to_thread(store.upsert_cell, analysis_id, row_id, column_id,
                                status="running", error="")
        try:
            value, model_used, tokens = await _run_ai_cell(col, row, computed, cols_by_id)
            await asyncio.to_thread(store.upsert_cell, analysis_id, row_id, column_id,
                                    value=value, status="done", error="",
                                    model_used=model_used, tokens_used=tokens)
            return {"status": "done", "row_id": row_id, "column_id": column_id,
                    "value": value, "model_used": model_used, "tokens_used": tokens}
        except Exception as e:  # noqa: BLE001
            await asyncio.to_thread(store.upsert_cell, analysis_id, row_id, column_id,
                                    status="error", error=str(e)[:1000])
            return {"status": "error", "row_id": row_id, "column_id": column_id,
                    "error": str(e)[:1000]}
    finally:
        _rerun_active.discard(analysis_id)


# ---------- read-only query over one analysis ----------

def _render_grid(full: dict, *, max_rows: int = 80) -> str:
    columns = sorted(full["columns"], key=lambda c: c.get("position", 0))
    rows = sorted(full["rows"], key=lambda r: r.get("position", 0))[:max_rows]
    cells = full["cells"]
    by_pair = {(c["row_id"], c["column_id"]): c for c in cells}
    head = ["Row"] + [c["name"] for c in columns]
    lines = [" | ".join(head), " | ".join("---" for _ in head)]
    for r in rows:
        line = [str(r.get("label") or r.get("entity_ref") or r["id"])[:60]]
        for c in columns:
            cell = by_pair.get((r["id"], c["id"]))
            val = (cell or {}).get("value") or ""
            val = val.replace("\n", " ").strip()
            if len(val) > 200:
                val = val[:200] + "…"
            line.append(val)
        lines.append(" | ".join(line))
    note = ""
    if len(full["rows"]) > max_rows:
        note = f"\n\n(Showing first {max_rows} of {len(full['rows'])} rows.)"
    return "\n".join(lines) + note


async def query_analysis(analysis_id: str, question: str, *, model: Optional[str] = None) -> dict:
    """Answer a natural-language question about ONE analysis using only its grid
    (read-only — no writes, no other tables)."""
    full = await asyncio.to_thread(store.get_full_analysis, analysis_id)
    if not full:
        return {"answer": f"No analysis found with id '{analysis_id}'.", "model": None}
    q = (question or "").strip()
    if not q:
        return {"answer": "Please provide a question.", "model": None}
    grid = _render_grid(full)
    title = (full["analysis"] or {}).get("title") or "Analysis"
    from langchain_core.messages import SystemMessage, HumanMessage
    llm = resolve_model(model or _DEFAULT_MODEL)
    system = (
        "You are a read-only analyst. Answer the user's question using ONLY the "
        "analysis table provided below. Never invent values not present in it. If "
        "the table does not contain the answer, say so plainly. Be concise and cite "
        "the relevant rows."
    )
    human = f"Analysis: {title}\n\n{grid}\n\nQuestion: {q}"
    ai = await llm.ainvoke([SystemMessage(content=system), HumanMessage(content=human)])
    answer = ai.content if isinstance(ai.content, str) else json.dumps(ai.content, default=str)
    return {"answer": answer, "model": validate_model(model or _DEFAULT_MODEL),
            "rows_considered": min(len(full["rows"]), 80)}
