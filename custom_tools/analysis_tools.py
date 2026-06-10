"""custom_tools/analysis_tools.py — agent tools for the Analysis feature (Task #52).

These let the agent build a spreadsheet-style analysis over many Salesforce
opportunities entirely from chat: create an analysis, add opportunity rows from
the local caches, add data + AI columns (each AI column with its own system
prompt and model), kick off / stop the Run-All engine, and ask read-only
questions about the result.

All DB access is delegated to analysis_store (table names are module constants
there — the agent never supplies a raw table name). Tools are async so LLM /
network work never blocks the server event loop; the DB calls are sync httpx so
they are wrapped in asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from langchain_core.tools import tool

import analysis_store as store
import analysis_engine as engine


def _ok(payload: dict) -> str:
    return json.dumps(payload, indent=2, default=str)


def _err(msg: str) -> str:
    return json.dumps({"error": msg}, indent=2, default=str)


@tool
async def analysis_create(title: str, description: str = "",
                          project_id: str = "", chat_id: str = "") -> str:
    """Create a new spreadsheet-style Analysis (rows = Salesforce opportunities,
    columns = data fields or AI-generated answers). Returns the analysis_id you
    will pass to the other analysis_* tools.

    Args:
        title: Short human title for the analysis.
        description: Optional longer description of what this analysis is for.
        project_id: Optional project id to scope it to.
        chat_id: Optional chat id this analysis belongs to.
    """
    try:
        a = await asyncio.to_thread(
            store.create_analysis, title,
            description=description or None,
            project_id=project_id or None,
            chat_id=chat_id or None,
        )
        return _ok({"analysis_id": a["id"], "title": a["title"], "status": a["status"]})
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@tool
async def analysis_add_opportunities(analysis_id: str, source: str = "opportunity_cache",
                                     stage: str = "", momentum: str = "",
                                     min_amount: Optional[float] = None,
                                     max_amount: Optional[float] = None,
                                     account_contains: str = "", name_contains: str = "",
                                     limit: int = 25) -> str:
    """Add opportunity rows to an analysis by pulling from a local cache.

    Args:
        analysis_id: The analysis to add rows to.
        source: 'opportunity_cache' (fast Salesforce mirror, default) or
                'opportunity_observatory' (long-form dossier headers).
        stage: Optional stage name substring filter.
        momentum: Optional momentum (Active/Moderate/Slow/Stalled) — opportunity_cache only.
        min_amount: Optional minimum deal amount.
        max_amount: Optional maximum deal amount.
        account_contains: Optional account-name substring filter.
        name_contains: Optional opportunity-name substring filter.
        limit: Max rows to add (default 25, capped at 200).
    """
    try:
        out = await asyncio.to_thread(
            store.add_rows_from_source, analysis_id, source,
            stage=stage or None, momentum=momentum or None,
            min_amount=min_amount, max_amount=max_amount,
            account_contains=account_contains or None,
            name_contains=name_contains or None, limit=limit,
        )
        return _ok(out)
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@tool
async def analysis_add_data_column(analysis_id: str, name: str, source_field: str) -> str:
    """Add a DATA column that surfaces a raw field from each opportunity's source
    record (e.g. amount, stage_name, health_score, account_name, owner_name).
    Cells are filled immediately for existing rows.

    Args:
        analysis_id: The analysis to add the column to.
        name: Display name for the column.
        source_field: Key in the row's source record to display.
    """
    try:
        col = await asyncio.to_thread(
            store.add_column, analysis_id, name, "data", config={"source_field": source_field})
        await asyncio.to_thread(store.populate_data_cells, analysis_id)
        return _ok({"column_id": col["id"], "name": col["name"], "type": "data",
                    "source_field": source_field, "position": col["position"]})
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@tool
async def analysis_add_ai_column(analysis_id: str, name: str, system_prompt: str,
                                 model: str = "", instructions: str = "",
                                 input_columns: Optional[list] = None,
                                 input_fields: Optional[list] = None) -> str:
    """Add an AI column. Each AI cell is one LLM call using THIS column's system
    prompt + model over the row's data. AI columns are computed left-to-right, so
    a column can reference earlier columns' outputs via input_columns. Cells start
    empty — call analysis_run to fill them.

    Args:
        analysis_id: The analysis to add the column to.
        name: Display name for the column.
        system_prompt: System prompt that defines this column's task.
        model: Optional 'provider:model' (e.g. 'openai:gpt-4o', 'anthropic:claude-3-5-haiku-latest').
               Defaults to the server default model.
        instructions: Optional extra per-cell instruction appended to the context.
        input_columns: Optional list of column ids whose values to feed in. If omitted,
                       all other columns + the raw source are provided.
        input_fields: Optional list of raw source field names to feed in.
    """
    try:
        model_norm = engine.validate_model(model) if model else None
        cfg: dict[str, Any] = {"system_prompt": system_prompt}
        if model_norm:
            cfg["model"] = model_norm
        if instructions:
            cfg["instructions"] = instructions
        if input_columns:
            cfg["input_columns"] = input_columns
        if input_fields:
            cfg["input_fields"] = input_fields
        col = await asyncio.to_thread(store.add_column, analysis_id, name, "ai", config=cfg)
        return _ok({"column_id": col["id"], "name": col["name"], "type": "ai",
                    "model": cfg.get("model", "default"), "position": col["position"]})
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@tool
async def analysis_edit_column(column_id: str, name: str = "", system_prompt: str = "",
                               model: str = "", instructions: str = "",
                               source_field: str = "") -> str:
    """Edit an existing column's name, AI system prompt, model, instructions, or
    data source_field. Only non-empty arguments are applied.

    Args:
        column_id: The column to edit.
        name: New display name (optional).
        system_prompt: New AI system prompt (optional, AI columns).
        model: New 'provider:model' (optional, AI columns).
        instructions: New per-cell instruction (optional, AI columns).
        source_field: New source field (optional, data columns).
    """
    try:
        existing = await asyncio.to_thread(store.get_column, column_id)
        if not existing:
            return _err(f"No column with id '{column_id}'.")
        patch: dict[str, Any] = {}
        if name:
            patch["name"] = name
        cfg_updates = {}
        if system_prompt:
            cfg_updates["system_prompt"] = system_prompt
        if model:
            cfg_updates["model"] = engine.validate_model(model)
        if instructions:
            cfg_updates["instructions"] = instructions
        if source_field:
            cfg_updates["source_field"] = source_field
        if cfg_updates:
            # Merge into existing config.
            cur_cfg = existing.get("config") or {}
            patch["config"] = {**cur_cfg, **cfg_updates}
        if not patch:
            return _err("Nothing to update — provide at least one field.")
        col = await asyncio.to_thread(store.update_column, column_id, patch)
        return _ok({"column_id": column_id, "updated": list(patch.keys()),
                    "config": (col or {}).get("config")})
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@tool
async def analysis_delete_column(column_id: str) -> str:
    """Delete a column (and its cells) from an analysis.

    Args:
        column_id: The column to delete.
    """
    try:
        await asyncio.to_thread(store.delete_column, column_id)
        return _ok({"deleted_column_id": column_id})
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@tool
async def analysis_delete_row(row_id: str) -> str:
    """Delete a row (and its cells) from an analysis.

    Args:
        row_id: The row to delete.
    """
    try:
        await asyncio.to_thread(store.delete_row, row_id)
        return _ok({"deleted_row_id": row_id})
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@tool
async def analysis_get(analysis_id: str) -> str:
    """Read the current schema + a small preview of an analysis: its columns
    (with types/config), row count, and the first few rows.

    Args:
        analysis_id: The analysis to read.
    """
    try:
        a = await asyncio.to_thread(store.get_analysis, analysis_id)
        if not a:
            return _err(f"No analysis with id '{analysis_id}'.")
        cols = await asyncio.to_thread(store.list_columns, analysis_id)
        rows = await asyncio.to_thread(store.list_rows, analysis_id, limit=5)
        run = await asyncio.to_thread(store.latest_run, analysis_id)
        return _ok({
            "analysis": {"id": a["id"], "title": a["title"], "status": a["status"]},
            "columns": [{"id": c["id"], "name": c["name"], "type": c["type"],
                         "position": c["position"], "config": c.get("config")} for c in cols],
            "row_preview": [{"id": r["id"], "label": r.get("label"),
                             "entity_ref": r.get("entity_ref")} for r in rows],
            "latest_run": run,
        })
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@tool
async def analysis_run(analysis_id: str) -> str:
    """Start the Run-All engine: fill every AI cell, row by row, left-to-right.
    Returns immediately; the grid updates live in Supabase. Only one run per
    analysis at a time.

    Args:
        analysis_id: The analysis to run.
    """
    try:
        return _ok(engine.start_run(analysis_id))
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@tool
async def analysis_stop(analysis_id: str) -> str:
    """Stop an in-progress Run-All for an analysis.

    Args:
        analysis_id: The analysis whose run to stop.
    """
    try:
        return _ok(engine.stop_run(analysis_id))
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@tool
async def analysis_rerun_cell(analysis_id: str, row_id: str = "", column_id: str = "",
                              cell_id: str = "") -> str:
    """Re-run a SINGLE AI cell (e.g. after editing its column's prompt/model).
    Identify the cell either by cell_id, or by both row_id and column_id.
    Data columns to the left and earlier AI columns feed it as inputs.

    Args:
        analysis_id: The analysis the cell belongs to.
        row_id: The cell's row (use with column_id).
        column_id: The cell's column (use with row_id).
        cell_id: The cell id (alternative to row_id + column_id).
    """
    try:
        out = await engine.rerun_cell(analysis_id, cell_id=cell_id or None,
                                      row_id=row_id or None, column_id=column_id or None)
        return _ok(out)
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@tool
async def analysis_edit_cell(cell_id: str, value: str) -> str:
    """Manually set a cell's value (overrides the AI result). Marks the cell done.

    Args:
        cell_id: The cell to edit.
        value: The new text value to store in the cell.
    """
    try:
        cell = await asyncio.to_thread(store.edit_cell, cell_id, value)
        if not cell:
            return _err(f"No cell with id '{cell_id}'.")
        return _ok({"cell_id": cell_id, "value": cell.get("value"), "status": cell.get("status")})
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@tool
async def analysis_query(analysis_id: str, question: str, model: str = "") -> str:
    """Ask a read-only natural-language question about a completed analysis. The
    answer comes ONLY from the analysis grid (no other data, no writes).

    Args:
        analysis_id: The analysis to query.
        question: The natural-language question.
        model: Optional 'provider:model' for the answering LLM.
    """
    try:
        out = await engine.query_analysis(analysis_id, question, model=model or None)
        return _ok(out)
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


# ---------- structured fetch / filter / search (no raw SQL) ----------

_SNIPPET_CHARS = 300
_PAGE_MAX = 100
_FILTER_OPS = {"empty", "not_empty", "equals", "contains", "gt", "gte", "lt", "lte"}


def _snippet(value: Optional[str]) -> dict:
    s = "" if value is None else str(value)
    if len(s) <= _SNIPPET_CHARS:
        return {"value": s, "truncated": False, "length": len(s)}
    return {"value": s[:_SNIPPET_CHARS], "truncated": True, "length": len(s)}


def _page_args(page: int, page_size: int) -> tuple[int, int]:
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 25), _PAGE_MAX))
    return page, page_size


def _page_meta(page: int, page_size: int, total: int, *, capped: bool = False) -> dict:
    total_pages = (total + page_size - 1) // page_size if total else 0
    has_more = page < total_pages
    meta = {
        "page": page, "page_size": page_size, "total": total,
        "total_pages": total_pages, "has_more": has_more,
        "next_page": (page + 1) if has_more else None,
    }
    if has_more:
        meta["hint"] = (
            f"Only page {page} of {total_pages} returned ({total} total). "
            f"Call this tool again with page={page + 1} to get the next batch; "
            "keep paging until has_more is false if you need everything."
        )
    if capped:
        meta["capped"] = True
        meta["capped_note"] = (
            "Result set hit the server cap; narrow your filter/search to be sure "
            "you are seeing all matches."
        )
    return meta


def _match(cell_value: Optional[str], op: str, target: str) -> bool:
    s = "" if cell_value is None else str(cell_value)
    if op == "empty":
        return s.strip() == ""
    if op == "not_empty":
        return s.strip() != ""
    if op == "equals":
        return s.strip().lower() == (target or "").strip().lower()
    if op == "contains":
        return (target or "").strip().lower() in s.lower()
    try:
        a, b = float(s), float(target)
    except (ValueError, TypeError):
        return False
    return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]


def _find_col(cols: list, name: str) -> Optional[dict]:
    name = (name or "").strip().lower()
    for c in cols:
        if (c.get("name") or "").strip().lower() == name:
            return c
    return None


def _target_column_ids(cols: list, *, data_only: bool,
                       include_columns: Optional[list]) -> Optional[list]:
    """Resolve which columns to RETURN. None = all columns."""
    if include_columns:
        wanted = {n.strip().lower() for n in include_columns if n and n.strip()}
        return [c["id"] for c in cols
                if (c.get("name") or "").strip().lower() in wanted]
    if data_only:
        return [c["id"] for c in cols if c.get("type") == "data"]
    return None


@tool
async def analysis_filter_rows(analysis_id: str, column_name: str = "", op: str = "",
                               value: str = "", row_label_contains: str = "",
                               data_only: bool = False, include_columns: Optional[list] = None,
                               page: int = 1, page_size: int = 25) -> str:
    """Find the analysis rows (opportunities) that match a filter, and return each
    matching row's cells. This is the right tool for "which rows have X" questions
    (e.g. blank Owner, Stage = 'Negotiation', Amount > 250000) and for opportunity
    search by name. Read-only; never writes.

    Filtering on a column value (optional): set `column_name` + `op` (+ `value`).
      op is one of: empty, not_empty, equals, contains, gt, gte, lt, lte
      (gt/gte/lt/lte compare numerically; equals/contains are case-insensitive).
    Opportunity search (optional): set `row_label_contains` to match the row label
      (the opportunity name). Can be combined with a column filter (both must hold).

    Output shaping:
      data_only=True returns ONLY data columns (skips the long AI columns) — use
        this to keep responses small when you just need the source fields.
      include_columns=["Owner","Stage"] returns only those named columns.
      Long cell values are returned as 300-char snippets with a `cell_id` you can
        pass to analysis_get_cells to read the full text.

    PAGINATION: results are paged (default 25 rows). The response has a
      `pagination` block; when `has_more` is true you MUST call again with the next
      `page` to see the remaining rows — do not assume the first page is complete.

    Args:
        analysis_id: The analysis to read.
        column_name: Column to filter on (optional).
        op: Filter operator (required if column_name is set).
        value: Comparison value for the operator (not needed for empty/not_empty).
        row_label_contains: Match rows whose label/opportunity name contains this.
        data_only: If true, return only data columns (omit AI columns).
        include_columns: Optional list of column names to return.
        page: 1-based page number.
        page_size: Rows per page (max 100).
    """
    try:
        page, page_size = _page_args(page, page_size)
        cols = await asyncio.to_thread(store.list_columns, analysis_id)
        if not cols:
            return _err(f"No columns for analysis '{analysis_id}' (does it exist?).")

        filt_col = None
        if column_name.strip() or op.strip():
            if not column_name.strip() or not op.strip():
                return _err("Provide both column_name and op to filter on a column.")
            op = op.strip().lower()
            if op not in _FILTER_OPS:
                return _err(f"op must be one of {sorted(_FILTER_OPS)}, got '{op}'.")
            filt_col = _find_col(cols, column_name)
            if not filt_col:
                names = [c.get("name") for c in cols]
                return _err(f"No column named '{column_name}'. Available: {names}")

        rows = await asyncio.to_thread(store.list_rows, analysis_id, limit=100000)
        valby_row = {}
        if filt_col:
            fcells = await asyncio.to_thread(store.list_cells_by_column,
                                             analysis_id, filt_col["id"])
            valby_row = {c["row_id"]: c.get("value") for c in fcells}

        label_needle = row_label_contains.strip().lower()
        matched = []
        for r in rows:
            if filt_col and not _match(valby_row.get(r["id"], ""), op, value):
                continue
            if label_needle and label_needle not in (r.get("label") or "").lower():
                continue
            matched.append(r)

        total = len(matched)
        start = (page - 1) * page_size
        page_rows = matched[start:start + page_size]

        ret_col_ids = _target_column_ids(cols, data_only=data_only,
                                         include_columns=include_columns)
        colmeta = {c["id"]: c for c in cols}
        cells = await asyncio.to_thread(
            store.list_cells_for_rows, analysis_id,
            [r["id"] for r in page_rows], column_ids=ret_col_ids)
        cells_by_row: dict = {}
        for cl in cells:
            cells_by_row.setdefault(cl["row_id"], {})[cl["column_id"]] = cl

        out_rows = []
        for r in page_rows:
            rc = cells_by_row.get(r["id"], {})
            cell_out = {}
            for cid, cl in rc.items():
                col = colmeta.get(cid, {})
                cell_out[col.get("name", cid)] = {
                    "cell_id": cl["id"], "type": col.get("type"),
                    "status": cl.get("status"), **_snippet(cl.get("value")),
                }
            out_rows.append({
                "row_id": r["id"], "label": r.get("label"),
                "entity_ref": r.get("entity_ref"), "cells": cell_out,
            })

        return _ok({
            "analysis_id": analysis_id,
            "filter": {"column_name": filt_col.get("name") if filt_col else None,
                       "op": op if filt_col else None, "value": value if filt_col else None,
                       "row_label_contains": row_label_contains or None,
                       "data_only": data_only,
                       "include_columns": include_columns or None},
            "rows": out_rows,
            "pagination": _page_meta(page, page_size, total),
        })
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@tool
async def analysis_search_cells(analysis_id: str, query: str, column_name: str = "",
                                data_only: bool = False, page: int = 1,
                                page_size: int = 25) -> str:
    """Full-text (case-insensitive substring) search across the cells of an
    analysis. Returns every cell whose value contains `query`, with its row label,
    column name, and a snippet. Use this to locate where something is mentioned
    (a person, competitor, keyword) anywhere in the grid. Read-only.

    Restrict the search with `column_name` (one column only) and/or `data_only`
      (search only data columns, skipping the long AI columns).
    Snippets are 300 chars; use the returned `cell_id` with analysis_get_cells to
      read a cell's full text.

    PAGINATION: results are paged (default 25 matches). Check the `pagination`
      block; when `has_more` is true, call again with the next `page` to get the
      rest of the matches.

    Args:
        analysis_id: The analysis to search.
        query: Substring to look for (case-insensitive).
        column_name: Optional single column to restrict the search to.
        data_only: If true, search only data columns (skip AI columns).
        page: 1-based page number.
        page_size: Matches per page (max 100).
    """
    try:
        if not (query or "").strip():
            return _err("query is required.")
        page, page_size = _page_args(page, page_size)
        cols = await asyncio.to_thread(store.list_columns, analysis_id)
        if not cols:
            return _err(f"No columns for analysis '{analysis_id}' (does it exist?).")
        colmeta = {c["id"]: c for c in cols}
        colpos = {c["id"]: c.get("position", 0) for c in cols}

        col_ids = None
        if column_name.strip():
            col = _find_col(cols, column_name)
            if not col:
                names = [c.get("name") for c in cols]
                return _err(f"No column named '{column_name}'. Available: {names}")
            col_ids = [col["id"]]
        elif data_only:
            col_ids = [c["id"] for c in cols if c.get("type") == "data"]

        cap = 2000
        hits = await asyncio.to_thread(store.search_cells, analysis_id, query,
                                       column_ids=col_ids, limit=cap)
        capped = len(hits) >= cap

        rows = await asyncio.to_thread(store.list_rows, analysis_id, limit=100000)
        rowmeta = {r["id"]: r for r in rows}
        rowpos = {r["id"]: r.get("position", 0) for r in rows}
        hits.sort(key=lambda c: (rowpos.get(c["row_id"], 0), colpos.get(c["column_id"], 0)))

        total = len(hits)
        start = (page - 1) * page_size
        page_hits = hits[start:start + page_size]

        matches = []
        for cl in page_hits:
            r = rowmeta.get(cl["row_id"], {})
            col = colmeta.get(cl["column_id"], {})
            matches.append({
                "cell_id": cl["id"], "row_id": cl["row_id"],
                "label": r.get("label"), "entity_ref": r.get("entity_ref"),
                "column_name": col.get("name"), "column_type": col.get("type"),
                **_snippet(cl.get("value")),
            })

        return _ok({
            "analysis_id": analysis_id, "query": query,
            "scope": {"column_name": column_name or None, "data_only": data_only},
            "matches": matches,
            "pagination": _page_meta(page, page_size, total, capped=capped),
        })
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@tool
async def analysis_get_cells(cell_ids: list) -> str:
    """Fetch the FULL (untruncated) value of one or more cells by their cell ids.
    Use this after analysis_filter_rows / analysis_search_cells return snippets and
    you need a cell's complete text (e.g. a long AI column write-up). Read-only.

    Args:
        cell_ids: List of cell ids to fetch (max 500).
    """
    try:
        if not cell_ids:
            return _err("cell_ids is required (a non-empty list).")
        ids = [str(c) for c in cell_ids if c]
        cells = await asyncio.to_thread(store.get_cells, ids)
        found = {c["id"]: c for c in cells}
        out = []
        for cid in ids:
            c = found.get(cid)
            if not c:
                out.append({"cell_id": cid, "error": "not found"})
                continue
            out.append({
                "cell_id": c["id"], "row_id": c.get("row_id"),
                "column_id": c.get("column_id"), "status": c.get("status"),
                "model_used": c.get("model_used"), "value": c.get("value"),
            })
        return _ok({"requested": len(ids), "returned": len(cells), "cells": out})
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")
