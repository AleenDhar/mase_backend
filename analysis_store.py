"""analysis_store.py — data-access + business logic for the Analysis feature
(Task #52).

Functional, dependency-light layer over Supabase's REST API using the
service-role key. Every table name is a MODULE CONSTANT, never taken from the
model/caller, so the agent tools that wrap these functions cannot pivot to other
tables. There is no generic "run arbitrary SQL" path here.

Used by:
  - the agent @tools in custom_tools/analysis_tools.py
  - the Run-All engine in analysis_engine.py
  - the REST endpoints in server.py

All functions are synchronous (httpx). Async callers should wrap them in
asyncio.to_thread to avoid blocking the event loop.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import httpx

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_SUPABASE_URL = (os.environ.get("SUPABASE_URL", "") or "").rstrip("/")
_SERVICE_KEY = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY", "")
)

# Hard-scoped table names — these are constants, never supplied by the model.
T_ANALYSES = "analyses"
T_COLUMNS = "analysis_columns"
T_ROWS = "analysis_rows"
T_CELLS = "analysis_cells"
T_RUNS = "analysis_runs"

# Allowlisted read sources for populating rows (the model picks one of these
# by a short key, never a raw table name).
ROW_SOURCES = {
    "opportunity_cache": {
        "table": "opportunity_cache",
        "id_field": "opportunity_id",
        "label_field": "opportunity_name",
        "select": (
            "opportunity_id,opportunity_name,account_name,amount,stage_name,close_date,"
            "probability,owner_name,is_closed,meetings_count,last_meeting_date,"
            "days_since_last_meeting,health_score,momentum,days_in_stage,risk_signals"
        ),
    },
    "opportunity_observatory": {
        "table": "opportunity_observatory",
        "id_field": "opportunity_id",
        "label_field": "name",
        # Header fields only — the long-form markdown dossier sections are huge
        # and are intentionally not copied into every analysis row.
        "select": "opportunity_id,name,opportunity_owner,close_date,amount,stage,account_name",
    },
}

COLUMN_TYPES = {"data", "ai"}

_TIMEOUT = 30.0


class AnalysisError(Exception):
    """Raised on configuration or REST failures (carries a readable message)."""


# ---------- low-level REST ----------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check() -> None:
    if not _SUPABASE_URL or not _SERVICE_KEY:
        raise AnalysisError("Supabase is not configured (SUPABASE_URL / service key missing).")


def _headers(prefer: str = "") -> dict:
    h = {
        "apikey": _SERVICE_KEY,
        "Authorization": f"Bearer {_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def _url(table: str) -> str:
    return f"{_SUPABASE_URL}/rest/v1/{table}"


def _raise_for(resp: httpx.Response, what: str):
    if resp.status_code >= 400:
        raise AnalysisError(f"{what}: HTTP {resp.status_code} {resp.text[:400]}")


def _insert(table: str, rows, *, returning: bool = True):
    _check()
    prefer = "return=representation" if returning else "return=minimal"
    resp = httpx.post(_url(table), headers=_headers(prefer), json=rows, timeout=_TIMEOUT)
    _raise_for(resp, f"insert into {table}")
    return resp.json() if returning else None


def _upsert(table: str, rows, on_conflict: str, *, returning: bool = True):
    _check()
    prefer = ("return=representation," if returning else "return=minimal,") + "resolution=merge-duplicates"
    resp = httpx.post(
        f"{_url(table)}?on_conflict={on_conflict}",
        headers=_headers(prefer),
        json=rows,
        timeout=_TIMEOUT,
    )
    _raise_for(resp, f"upsert into {table}")
    return resp.json() if returning else None


def _patch(table: str, filters: dict, patch: dict, *, returning: bool = True):
    _check()
    params = "&".join(f"{k}=eq.{v}" for k, v in filters.items())
    prefer = "return=representation" if returning else "return=minimal"
    resp = httpx.patch(
        f"{_url(table)}?{params}", headers=_headers(prefer), json=patch, timeout=_TIMEOUT
    )
    _raise_for(resp, f"update {table}")
    return resp.json() if returning else None


def _delete(table: str, filters: dict) -> None:
    _check()
    params = "&".join(f"{k}=eq.{v}" for k, v in filters.items())
    resp = httpx.delete(f"{_url(table)}?{params}", headers=_headers(), timeout=_TIMEOUT)
    _raise_for(resp, f"delete from {table}")


def _select(table: str, *, select: str = "*", filters: Optional[list[str]] = None,
            order: Optional[str] = None, limit: Optional[int] = None) -> list:
    _check()
    params = [f"select={select}"]
    if filters:
        params.extend(filters)
    if order:
        params.append(f"order={order}")
    if limit:
        params.append(f"limit={int(limit)}")
    resp = httpx.get(f"{_url(table)}?{'&'.join(params)}", headers=_headers(), timeout=_TIMEOUT)
    _raise_for(resp, f"select from {table}")
    return resp.json()


def _first(rows):
    return rows[0] if rows else None


def _next_position(table: str, analysis_id: str) -> int:
    rows = _select(table, select="position",
                   filters=[f"analysis_id=eq.{analysis_id}"],
                   order="position.desc", limit=1)
    if rows and rows[0].get("position") is not None:
        return int(rows[0]["position"]) + 1
    return 0


# ---------- analyses ----------

def create_analysis(title: str, *, description: Optional[str] = None,
                    project_id: Optional[str] = None, chat_id: Optional[str] = None,
                    created_by: Optional[str] = None,
                    source_config: Optional[dict] = None) -> dict:
    row = {
        "title": (title or "Untitled analysis").strip() or "Untitled analysis",
        "description": description,
        "project_id": project_id,
        "chat_id": chat_id,
        "created_by": created_by,
        "source_config": source_config or {},
        "status": "draft",
    }
    return _first(_insert(T_ANALYSES, row))


def get_analysis(analysis_id: str) -> Optional[dict]:
    return _first(_select(T_ANALYSES, filters=[f"id=eq.{analysis_id}"], limit=1))


def list_analyses(*, project_id: Optional[str] = None, chat_id: Optional[str] = None,
                  limit: int = 50) -> list:
    filters = []
    if project_id:
        filters.append(f"project_id=eq.{project_id}")
    if chat_id:
        filters.append(f"chat_id=eq.{chat_id}")
    return _select(T_ANALYSES, filters=filters, order="updated_at.desc",
                   limit=max(1, min(int(limit or 50), 200)))


def update_analysis(analysis_id: str, patch: dict) -> Optional[dict]:
    patch = {**patch, "updated_at": _now()}
    return _first(_patch(T_ANALYSES, {"id": analysis_id}, patch))


def delete_analysis(analysis_id: str) -> None:
    _delete(T_ANALYSES, {"id": analysis_id})


# ---------- columns ----------

def add_column(analysis_id: str, name: str, col_type: str, *,
               config: Optional[dict] = None, position: Optional[int] = None) -> dict:
    if col_type not in COLUMN_TYPES:
        raise AnalysisError(f"col_type must be one of {sorted(COLUMN_TYPES)}, got '{col_type}'.")
    if not (name or "").strip():
        raise AnalysisError("Column name is required.")
    pos = position if position is not None else _next_position(T_COLUMNS, analysis_id)
    row = {
        "analysis_id": analysis_id,
        "name": name.strip(),
        "type": col_type,
        "config": config or {},
        "position": pos,
    }
    return _first(_insert(T_COLUMNS, row))


def list_columns(analysis_id: str) -> list:
    return _select(T_COLUMNS, filters=[f"analysis_id=eq.{analysis_id}"], order="position.asc")


def get_column(column_id: str) -> Optional[dict]:
    return _first(_select(T_COLUMNS, filters=[f"id=eq.{column_id}"], limit=1))


def update_column(column_id: str, patch: dict) -> Optional[dict]:
    patch = {**patch, "updated_at": _now()}
    return _first(_patch(T_COLUMNS, {"id": column_id}, patch))


def delete_column(column_id: str) -> None:
    _delete(T_COLUMNS, {"id": column_id})


# ---------- rows ----------

def add_rows(analysis_id: str, rows: list[dict]) -> list:
    if not rows:
        return []
    start = _next_position(T_ROWS, analysis_id)
    payload = []
    for i, r in enumerate(rows):
        payload.append({
            "analysis_id": analysis_id,
            "position": start + i,
            "entity_ref": r.get("entity_ref"),
            "label": r.get("label"),
            "source": r.get("source") or {},
        })
    return _insert(T_ROWS, payload)


def add_rows_from_source(analysis_id: str, source: str, *,
                         stage: Optional[str] = None, momentum: Optional[str] = None,
                         min_amount: Optional[float] = None, max_amount: Optional[float] = None,
                         account_contains: Optional[str] = None,
                         name_contains: Optional[str] = None,
                         is_closed: Optional[bool] = None,
                         limit: int = 25) -> dict:
    """Pull opportunities from an allowlisted read source (opportunity_cache or
    opportunity_observatory) and add them as analysis rows."""
    spec = ROW_SOURCES.get(source)
    if not spec:
        raise AnalysisError(f"source must be one of {sorted(ROW_SOURCES)}, got '{source}'.")
    stage_field = "stage_name" if source == "opportunity_cache" else "stage"
    filters = []
    if stage:
        filters.append(f"{stage_field}=ilike.*{stage}*")
    if momentum and source == "opportunity_cache":
        filters.append(f"momentum=eq.{momentum}")
    if min_amount is not None:
        filters.append(f"amount=gte.{min_amount}")
    if max_amount is not None:
        filters.append(f"amount=lte.{max_amount}")
    if account_contains:
        filters.append(f"account_name=ilike.*{account_contains}*")
    if name_contains:
        filters.append(f"{spec['label_field']}=ilike.*{name_contains}*")
    if is_closed is not None and source == "opportunity_cache":
        filters.append(f"is_closed=eq.{str(bool(is_closed)).lower()}")

    lim = max(1, min(int(limit or 25), 200))
    found = _select(spec["table"], select=spec["select"], filters=filters,
                    order="amount.desc", limit=lim)
    rows = [{
        "entity_ref": r.get(spec["id_field"]),
        "label": r.get(spec["label_field"]),
        "source": r,
    } for r in found]
    added = add_rows(analysis_id, rows)
    populate_data_cells(analysis_id)
    return {"source": source, "found": len(found), "added": len(added)}


def list_rows(analysis_id: str, *, limit: int = 1000) -> list:
    return _select(T_ROWS, filters=[f"analysis_id=eq.{analysis_id}"],
                   order="position.asc", limit=limit)


def delete_row(row_id: str) -> None:
    _delete(T_ROWS, {"id": row_id})


# ---------- cells ----------

def upsert_cell(analysis_id: str, row_id: str, column_id: str, *,
                value: Optional[str] = None, status: Optional[str] = None,
                error: Optional[str] = None, model_used: Optional[str] = None,
                tokens_used: Optional[int] = None) -> Optional[dict]:
    row = {
        "analysis_id": analysis_id,
        "row_id": row_id,
        "column_id": column_id,
        "updated_at": _now(),
    }
    if value is not None:
        row["value"] = value
    if status is not None:
        row["status"] = status
    if error is not None:
        row["error"] = error
    if model_used is not None:
        row["model_used"] = model_used
    if tokens_used is not None:
        row["tokens_used"] = tokens_used
    return _first(_upsert(T_CELLS, row, on_conflict="row_id,column_id"))


def list_cells(analysis_id: str, *, limit: int = 50000) -> list:
    return _select(T_CELLS, filters=[f"analysis_id=eq.{analysis_id}"], limit=limit)


# ---------- filtered / searchable reads (agent fetch tools) ----------

_CELL_COLS = "id,row_id,column_id,value,status,model_used,error,updated_at"


def count_rows(analysis_id: str) -> int:
    """Total number of rows in an analysis (for pagination math)."""
    return len(_select(T_ROWS, select="id",
                       filters=[f"analysis_id=eq.{analysis_id}"], limit=100000))


def list_cells_by_column(analysis_id: str, column_id: str, *, limit: int = 100000) -> list:
    """All cells for ONE column (bounded by row count)."""
    return _select(T_CELLS, select=_CELL_COLS,
                   filters=[f"analysis_id=eq.{analysis_id}",
                            f"column_id=eq.{column_id}"], limit=limit)


def list_cells_for_rows(analysis_id: str, row_ids: list[str], *,
                        column_ids: Optional[list[str]] = None,
                        limit: int = 100000) -> list:
    """Cells for a specific set of rows, optionally restricted to some columns.
    `column_ids=None` means all columns; `column_ids=[]` means none (returns [])."""
    if not row_ids:
        return []
    filters = [f"analysis_id=eq.{analysis_id}",
               f"row_id=in.({','.join(row_ids)})"]
    if column_ids is not None:
        if not column_ids:
            return []
        filters.append(f"column_id=in.({','.join(column_ids)})")
    return _select(T_CELLS, select=_CELL_COLS, filters=filters, limit=limit)


def search_cells(analysis_id: str, query: str, *,
                 column_ids: Optional[list[str]] = None,
                 limit: int = 2000) -> list:
    """Substring (case-insensitive) match over cell values. The match is pushed
    to the database (ilike). `column_ids` optionally restricts which columns are
    searched (None=all, []=none)."""
    # URL-encode the model-supplied term so special chars (& , ) % space) can't
    # break PostgREST filter parsing; outer * stay as ilike wildcards.
    safe_q = quote(query, safe="")
    filters = [f"analysis_id=eq.{analysis_id}", f"value=ilike.*{safe_q}*"]
    if column_ids is not None:
        if not column_ids:
            return []
        filters.append(f"column_id=in.({','.join(column_ids)})")
    return _select(T_CELLS, select=_CELL_COLS, filters=filters, limit=limit)


def get_cells(cell_ids: list[str], *, limit: int = 500) -> list:
    """Fetch full (untruncated) cells by a list of cell ids. Ids are validated as
    UUIDs (model-supplied) and fetched in chunks to stay under URL length limits."""
    ids = [str(c) for c in (cell_ids or []) if _UUID_RE.match(str(c))][:limit]
    if not ids:
        return []
    out: list = []
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        out.extend(_select(T_CELLS, filters=[f"id=in.({','.join(chunk)})"],
                           limit=len(chunk)))
    return out


def set_cells_status(analysis_id: str, pairs: list, status: str) -> int:
    """Bulk-set status (e.g. 'pending') for many (row_id, column_id) cells."""
    payload = [{
        "analysis_id": analysis_id, "row_id": r, "column_id": c,
        "status": status, "error": "", "updated_at": _now(),
    } for (r, c) in pairs]
    for i in range(0, len(payload), 500):
        _upsert(T_CELLS, payload[i:i + 500], on_conflict="row_id,column_id", returning=False)
    return len(payload)


def edit_cell(cell_id: str, value: str) -> Optional[dict]:
    """Manual user edit of a single cell — marks it done, clears error/usage."""
    return _first(_patch(T_CELLS, {"id": cell_id}, {
        "value": value, "status": "done", "error": "",
        "model_used": "manual", "tokens_used": None, "updated_at": _now(),
    }))


def _data_value(col: dict, row: dict) -> str:
    cfg = col.get("config") or {}
    field = cfg.get("source_field")
    src = row.get("source") or {}
    if field and field in src:
        v = src[field]
        if v is None:
            return ""
        return v if isinstance(v, str) else json.dumps(v, default=str)
    return ""


def populate_data_cells(analysis_id: str) -> int:
    """Ensure every (data column × row) cell holds the value from row.source.
    Idempotent (upsert on row_id,column_id). Returns number of cells written."""
    cols = [c for c in list_columns(analysis_id) if c.get("type") == "data"]
    if not cols:
        return 0
    rows = list_rows(analysis_id)
    payload = []
    for col in cols:
        for row in rows:
            payload.append({
                "analysis_id": analysis_id,
                "row_id": row["id"],
                "column_id": col["id"],
                "value": _data_value(col, row),
                "status": "done",
                "updated_at": _now(),
            })
    if not payload:
        return 0
    # Batch to keep request bodies reasonable.
    written = 0
    for i in range(0, len(payload), 500):
        chunk = payload[i:i + 500]
        _upsert(T_CELLS, chunk, on_conflict="row_id,column_id", returning=False)
        written += len(chunk)
    return written


# ---------- runs ----------

def create_run(analysis_id: str, cells_total: int) -> dict:
    return _first(_insert(T_RUNS, {
        "analysis_id": analysis_id,
        "status": "running",
        "cells_total": cells_total,
        "cells_done": 0,
        "cells_error": 0,
    }))


def update_run(run_id: str, patch: dict) -> Optional[dict]:
    return _first(_patch(T_RUNS, {"id": run_id}, patch))


def list_runs(analysis_id: str, *, limit: int = 20) -> list:
    return _select(T_RUNS, filters=[f"analysis_id=eq.{analysis_id}"],
                   order="started_at.desc", limit=limit)


def latest_run(analysis_id: str) -> Optional[dict]:
    return _first(list_runs(analysis_id, limit=1))


# ---------- composite read ----------

def get_cell(cell_id: str) -> Optional[dict]:
    return _first(_select(T_CELLS, filters=[f"id=eq.{cell_id}"], limit=1))


def get_cell_by_pair(analysis_id: str, row_id: str, column_id: str) -> Optional[dict]:
    return _first(_select(T_CELLS, filters=[
        f"analysis_id=eq.{analysis_id}", f"row_id=eq.{row_id}",
        f"column_id=eq.{column_id}"], limit=1))


def get_full_analysis(analysis_id: str) -> Optional[dict]:
    """Initial-load snapshot: header + columns + rows + cells + runs."""
    analysis = get_analysis(analysis_id)
    if not analysis:
        return None
    runs = list_runs(analysis_id)
    return {
        "analysis": analysis,
        "columns": list_columns(analysis_id),
        "rows": list_rows(analysis_id),
        "cells": list_cells(analysis_id),
        "runs": runs,
        "latest_run": runs[0] if runs else None,
    }
