"""custom_tools/jarvis_tools.py — cross-analysis read tools for the Jarvis agent.

Jarvis is NOT tied to a single analysis. These tools operate over the GLOBAL set
of "enabled" analyses configured in jarvis_settings (the settings-tab toggles), so
the agent can search / filter / read across MANY analyses at once while staying
strictly scoped to the enabled set. Everything here is read-only; the underlying
table names stay hard-coded constants in analysis_store (no raw SQL path).

These auto-register with the CustomToolsLoader (any module-level @tool is picked
up); no server.py wiring is needed beyond a restart.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from langchain_core.tools import tool

import analysis_store as store
import jarvis_store

_SNIPPET_CHARS = 300
_PAGE_MAX = 100
_SEARCH_CAP_PER_ANALYSIS = 1000
_FILTER_OPS = {"empty", "not_empty", "equals", "contains", "gt", "gte", "lt", "lte"}


def _ok(payload: dict) -> str:
    return json.dumps({"ok": True, **payload}, default=str)


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg})


def _snippet(value: Optional[str]) -> dict:
    s = "" if value is None else str(value)
    if len(s) <= _SNIPPET_CHARS:
        return {"value": s, "truncated": False}
    return {"value": s[:_SNIPPET_CHARS], "truncated": True, "length": len(s)}


def _page_args(page: int, page_size: int) -> tuple[int, int]:
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 25), _PAGE_MAX))
    return page, page_size


def _page_meta(page: int, page_size: int, total: int, *, capped: bool = False) -> dict:
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    has_more = page < total_pages
    meta = {
        "page": page, "page_size": page_size, "total": total,
        "total_pages": total_pages, "has_more": has_more,
        "next_page": page + 1 if has_more else None,
        "hint": ("More results — call again with the next page."
                 if has_more else "All results returned."),
    }
    if capped:
        meta["capped"] = True
        meta["capped_note"] = ("Some analyses hit the per-analysis match cap; "
                               "narrow your query for completeness.")
    return meta


def _num(v) -> Optional[float]:
    try:
        return float(str(v).replace(",", "").replace("$", "").replace("%", "").strip())
    except Exception:  # noqa: BLE001
        return None


def _match(cell_value: Optional[str], op: str, target: str) -> bool:
    v = "" if cell_value is None else str(cell_value)
    if op == "empty":
        return v.strip() == ""
    if op == "not_empty":
        return v.strip() != ""
    if op == "equals":
        return v.strip().lower() == str(target).strip().lower()
    if op == "contains":
        return str(target).strip().lower() in v.lower()
    a, b = _num(v), _num(target)
    if a is None or b is None:
        return False
    if op == "gt":
        return a > b
    if op == "gte":
        return a >= b
    if op == "lt":
        return a < b
    if op == "lte":
        return a <= b
    return False


def _find_col(cols: list, name: str) -> Optional[dict]:
    n = (name or "").strip().lower()
    for c in cols:
        if (c.get("name") or "").strip().lower() == n:
            return c
    return None


def _target_col_ids(cols: list, data_only: bool,
                    include_columns: Optional[list]) -> Optional[list]:
    if include_columns:
        wanted = {str(n).strip().lower() for n in include_columns}
        return [c["id"] for c in cols
                if (c.get("name") or "").strip().lower() in wanted]
    if data_only:
        return [c["id"] for c in cols if c.get("type") == "data"]
    return None


@tool
async def jarvis_list_analyses() -> str:
    """List the analyses Jarvis is currently allowed to read (the enabled set from
    the Jarvis settings tab). Call this FIRST to see your scope: each entry has the
    analysis_id, title and status. Every other jarvis_* tool searches ONLY these
    analyses. Read-only."""
    try:
        items = await asyncio.to_thread(jarvis_store.get_enabled_analyses)
        return _ok({
            "count": len(items),
            "analyses": items,
            "note": ("No analyses are enabled in Jarvis settings — ask the user to "
                     "enable some in the settings tab." if not items else
                     "jarvis_search and jarvis_filter_rows span ALL of these at once."),
        })
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@tool
async def jarvis_search(query: str, data_only: bool = False,
                        page: int = 1, page_size: int = 25) -> str:
    """Search for a substring (case-insensitive) across the cells of ALL enabled
    analyses at once. Returns each matching cell tagged with its analysis title,
    row label, column name and a snippet. This is the primary way to find where
    something (a person, competitor, keyword) is mentioned anywhere in Jarvis's
    analyses. Read-only.

    data_only=True searches only data columns (skips the long AI columns).
    Long values come back as 300-char snippets with a `cell_id` — pass it to
      jarvis_get_cells to read the full text.
    PAGINATION: check the `pagination` block; while `has_more` is true you MUST
      call again with the next `page`.

    Args:
        query: Substring to look for (case-insensitive).
        data_only: If true, search only data columns (skip AI columns).
        page: 1-based page number.
        page_size: Matches per page (max 100).
    """
    try:
        if not (query or "").strip():
            return _err("query is required.")
        page, page_size = _page_args(page, page_size)
        enabled = await asyncio.to_thread(jarvis_store.get_enabled_analyses)
        if not enabled:
            return _err("No analyses are enabled in Jarvis settings — nothing to search.")

        all_hits: list = []
        capped = False
        for a in enabled:
            aid = a["id"]
            cols = await asyncio.to_thread(store.list_columns, aid)
            if not cols:
                continue
            colmeta = {c["id"]: c for c in cols}
            col_ids = _target_col_ids(cols, data_only, None)
            hits = await asyncio.to_thread(store.search_cells, aid, query,
                                           column_ids=col_ids,
                                           limit=_SEARCH_CAP_PER_ANALYSIS)
            if len(hits) >= _SEARCH_CAP_PER_ANALYSIS:
                capped = True
            rows = await asyncio.to_thread(store.list_rows, aid, limit=100000)
            rowmeta = {r["id"]: r for r in rows}
            for cl in hits:
                r = rowmeta.get(cl["row_id"], {})
                col = colmeta.get(cl["column_id"], {})
                all_hits.append({
                    "analysis_id": aid, "analysis_title": a.get("title"),
                    "cell_id": cl["id"], "row_id": cl["row_id"],
                    "label": r.get("label"), "entity_ref": r.get("entity_ref"),
                    "column_name": col.get("name"), "column_type": col.get("type"),
                    **_snippet(cl.get("value")),
                })

        total = len(all_hits)
        start = (page - 1) * page_size
        page_hits = all_hits[start:start + page_size]
        return _ok({
            "query": query,
            "scope": {"analyses": len(enabled), "data_only": data_only},
            "matches": page_hits,
            "pagination": _page_meta(page, page_size, total, capped=capped),
        })
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@tool
async def jarvis_filter_rows(column_name: str = "", op: str = "", value: str = "",
                             row_label_contains: str = "", data_only: bool = False,
                             include_columns: Optional[list] = None,
                             page: int = 1, page_size: int = 25) -> str:
    """Find rows (opportunities) across ALL enabled analyses that match a filter,
    returning each matching row's cells tagged with its analysis. Use this for
    "which opportunities have X" across everything Jarvis can see. Read-only.

    Column filter (optional): set column_name + op (+ value). op is one of:
      empty, not_empty, equals, contains, gt, gte, lt, lte (gt/gte/lt/lte compare
      numerically; equals/contains are case-insensitive). The filter is applied
      only in analyses that actually have a column of that name (the rest are
      reported in `skipped_missing_column`).
    Opportunity search (optional): set row_label_contains to match the row label
      (the opportunity name). Can be combined with a column filter.
    Output shaping: data_only=True returns only data columns;
      include_columns=["Owner","Stage"] returns only those named columns. Long
      values are snippets with a `cell_id` for jarvis_get_cells.
    PAGINATION: honour the `pagination` block; page through while `has_more`.

    Args:
        column_name: Column to filter on (optional).
        op: Filter operator (required if column_name is set).
        value: Comparison value (not needed for empty/not_empty).
        row_label_contains: Match rows whose label/opportunity name contains this.
        data_only: If true, return only data columns.
        include_columns: Optional list of column names to return.
        page: 1-based page number.
        page_size: Rows per page (max 100).
    """
    try:
        page, page_size = _page_args(page, page_size)
        op = op.strip().lower()
        if column_name.strip() or op:
            if not column_name.strip() or not op:
                return _err("Provide both column_name and op to filter on a column.")
            if op not in _FILTER_OPS:
                return _err(f"op must be one of {sorted(_FILTER_OPS)}, got '{op}'.")

        enabled = await asyncio.to_thread(jarvis_store.get_enabled_analyses)
        if not enabled:
            return _err("No analyses are enabled in Jarvis settings — nothing to filter.")

        label_needle = row_label_contains.strip().lower()
        per_analysis: dict = {}
        matched: list = []          # flat [(aid, row)] preserving order
        skipped_missing_col: list = []

        for a in enabled:
            aid = a["id"]
            cols = await asyncio.to_thread(store.list_columns, aid)
            if not cols:
                continue
            filt_col = None
            if op:
                filt_col = _find_col(cols, column_name)
                if not filt_col:
                    skipped_missing_col.append(a.get("title") or aid)
                    continue
            valby_row = {}
            if filt_col:
                fcells = await asyncio.to_thread(store.list_cells_by_column,
                                                 aid, filt_col["id"])
                valby_row = {c["row_id"]: c.get("value") for c in fcells}
            rows = await asyncio.to_thread(store.list_rows, aid, limit=100000)
            per_analysis[aid] = {
                "colmeta": {c["id"]: c for c in cols},
                "ret_col_ids": _target_col_ids(cols, data_only, include_columns),
                "title": a.get("title"),
            }
            for r in rows:
                if filt_col and not _match(valby_row.get(r["id"], ""), op, value):
                    continue
                if label_needle and label_needle not in (r.get("label") or "").lower():
                    continue
                matched.append((aid, r))

        total = len(matched)
        start = (page - 1) * page_size
        page_slice = matched[start:start + page_size]

        by_aid: dict = {}
        for aid, r in page_slice:
            by_aid.setdefault(aid, []).append(r)

        out_rows: list = []
        for aid, rws in by_aid.items():
            info = per_analysis[aid]
            cells = await asyncio.to_thread(
                store.list_cells_for_rows, aid, [r["id"] for r in rws],
                column_ids=info["ret_col_ids"])
            cells_by_row: dict = {}
            for cl in cells:
                cells_by_row.setdefault(cl["row_id"], {})[cl["column_id"]] = cl
            for r in rws:
                rc = cells_by_row.get(r["id"], {})
                cell_out = {}
                for cid, cl in rc.items():
                    col = info["colmeta"].get(cid, {})
                    cell_out[col.get("name", cid)] = {
                        "cell_id": cl["id"], "type": col.get("type"),
                        "status": cl.get("status"), **_snippet(cl.get("value")),
                    }
                out_rows.append({
                    "analysis_id": aid, "analysis_title": info["title"],
                    "row_id": r["id"], "label": r.get("label"),
                    "entity_ref": r.get("entity_ref"), "cells": cell_out,
                })

        order = {(aid, r["id"]): i for i, (aid, r) in enumerate(page_slice)}
        out_rows.sort(key=lambda o: order.get((o["analysis_id"], o["row_id"]), 0))

        return _ok({
            "filter": {"column_name": column_name or None, "op": op or None,
                       "value": value or None,
                       "row_label_contains": row_label_contains or None,
                       "data_only": data_only,
                       "include_columns": include_columns or None},
            "scope": {"analyses": len(enabled)},
            "skipped_missing_column": skipped_missing_col or None,
            "rows": out_rows,
            "pagination": _page_meta(page, page_size, total),
        })
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@tool
async def jarvis_get_cells(cell_ids: list) -> str:
    """Fetch the FULL (untruncated) value of one or more cells by their cell ids,
    across the enabled analyses. Use after jarvis_search / jarvis_filter_rows
    return snippets and you need the complete text. Cell ids that don't belong to
    an enabled analysis are ignored (reported in `not_found_or_out_of_scope`).
    Read-only.

    Args:
        cell_ids: List of cell ids (UUIDs) to fetch in full.
    """
    try:
        if not cell_ids:
            return _err("cell_ids is required.")
        enabled_ids = set(await asyncio.to_thread(jarvis_store.get_enabled_analysis_ids))
        if not enabled_ids:
            return _err("No analyses are enabled in Jarvis settings.")
        cells = await asyncio.to_thread(store.get_cells, list(cell_ids))
        out = []
        for cl in cells:
            if cl.get("analysis_id") not in enabled_ids:
                continue
            out.append({
                "cell_id": cl.get("id"), "analysis_id": cl.get("analysis_id"),
                "row_id": cl.get("row_id"), "column_id": cl.get("column_id"),
                "status": cl.get("status"), "value": cl.get("value"),
            })
        found = {c["cell_id"] for c in out}
        missing = [str(c) for c in cell_ids if str(c) not in found]
        return _ok({
            "count": len(out), "cells": out,
            "not_found_or_out_of_scope": missing or None,
        })
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")
