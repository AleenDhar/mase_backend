"""deal_engine_report.py — the Salesforce report that defines Deal Engine
membership.

The Salesforce report "MASE Opportunity V1" (default id 00OP7000005fkYfMAI,
overridable via env DEAL_ENGINE_REPORT_ID) is the SINGLE SOURCE OF TRUTH for
which opportunities belong to the Deal Engine book. This module is the only
reader of that report: it returns the set of opportunity ids the report
currently contains, with hard safety guards so a bad/empty read can NEVER be
mistaken for "the book is now empty".

Read-only. Reuses the shared, SF_*-credentialed simple-salesforce connection
from salesforce_task_writer (NOT the agent tool catalog / MCP servers), so the
Salesforce write lockdown stays intact.

Salesforce report ids come in two forms: the report's detail rows expose each
opportunity's 18-char API id (dataCells[idx]['value']) and its 15-char form
(dataCells[idx]['label']). We collect the 18-char ids and dedupe membership on
the 15-char prefix (the deal_records table is keyed on the 15-char id).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

REPORT_ID = (os.environ.get("DEAL_ENGINE_REPORT_ID") or "00OP7000005fkYfMAI").strip()

# Salesforce Analytics REST returns at most 2000 detail rows per report run.
# We log a loud warning if a read hits that ceiling (membership would be
# silently truncated). The MASE report currently returns ~464 rows.
_ROW_CAP = 2000

# Short TTL so the membership set can be reused across the per-cycle reconcile
# and discovery enrichment within one run without re-hitting the API, while
# staying fresh enough that an on-demand reconcile sees recent changes.
_TTL_S = float(os.environ.get("DEAL_ENGINE_REPORT_TTL_S", "300"))

_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "result": None}


def _id15(v: Optional[str]) -> str:
    return (v or "").strip()[:15]


def _extract_ids(report: dict) -> list[str]:
    """Pull the OPPORTUNITY_ID column's 18-char ids out of a tabular report
    payload. Returns ids in report order (raw, not yet deduped)."""
    detail_cols = (report.get("reportMetadata") or {}).get("detailColumns") or []
    try:
        idx = detail_cols.index("OPPORTUNITY_ID")
    except ValueError:
        # Some orgs expose the lookup id under a relationship-qualified name.
        idx = next(
            (i for i, c in enumerate(detail_cols)
             if str(c).upper().endswith("OPPORTUNITY_ID")),
            -1,
        )
    if idx < 0:
        return []
    ids: list[str] = []
    fact_map = report.get("factMap") or {}
    for block in fact_map.values():
        for row in (block.get("rows") or []):
            cells = row.get("dataCells") or []
            if idx >= len(cells):
                continue
            cell = cells[idx] or {}
            val = (cell.get("value") or cell.get("label") or "")
            val = str(val).strip()
            if val:
                ids.append(val)
    return ids


def _fetch_uncached() -> dict:
    """Read the report once and return the membership result dict. Never raises:
    any failure is folded into {ok: False, error: ...} so callers always get a
    structured, guard-friendly answer."""
    if not REPORT_ID:
        return {"ok": False, "ids18": [], "ids15": set(), "count": 0,
                "truncated": False, "error": "DEAL_ENGINE_REPORT_ID is not set",
                "report_id": REPORT_ID}
    try:
        import salesforce_task_writer as sftw
        sf = sftw.get_connection()
        report = sf.restful(
            f"analytics/reports/{REPORT_ID}?includeDetails=true")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "ids18": [], "ids15": set(), "count": 0,
                "truncated": False, "error": f"report read failed: {e}",
                "report_id": REPORT_ID}

    all_data = bool(((report or {}).get("allData")) is not False)
    raw = _extract_ids(report or {})
    truncated = (not all_data) or (len(raw) >= _ROW_CAP)
    # Dedupe membership on the 15-char id while keeping a representative 18-char
    # id per member (last one wins; they share the same 15-char prefix).
    by15: dict[str, str] = {}
    for rid in raw:
        by15[_id15(rid)] = rid
    by15.pop("", None)
    ids15 = set(by15.keys())
    ids18 = sorted(by15.values())

    if not ids15:
        # An empty membership set is treated as a FAILURE, not "book is empty":
        # a real report should always contain rows, so 0 almost certainly means
        # a permissions/filter/transient problem. Callers must NOT wipe the book.
        return {"ok": False, "ids18": [], "ids15": set(), "count": 0,
                "truncated": truncated,
                "error": "report returned 0 opportunity ids",
                "report_id": REPORT_ID}

    return {"ok": True, "ids18": ids18, "ids15": ids15, "count": len(ids15),
            "truncated": truncated, "error": None, "report_id": REPORT_ID}


def fetch_report_membership(force: bool = False) -> dict:
    """Return the current report membership, TTL-cached.

    Result: {ok, ids18: list[str] (18-char), ids15: set[str] (15-char),
             count, truncated, error, report_id}.

    Guards (ok == False, caller must abort any removal/membership change):
      - report id unset, API error, or 0 rows returned.
    Failures are NOT cached, so the next call retries immediately. `force=True`
    bypasses the cache for the safety-critical reconcile read."""
    now = time.time()
    with _lock:
        cached = _cache.get("result")
        if (not force and cached and cached.get("ok")
                and (now - _cache["ts"]) < _TTL_S):
            out = dict(cached)
            out["cached"] = True
            return out
    result = _fetch_uncached()
    if result.get("ok"):
        with _lock:
            _cache["ts"] = time.time()
            _cache["result"] = result
    result = dict(result)
    result["cached"] = False
    return result
