"""custom_tools/dashboard_tools.py — agent tools for analysis dashboards (Task #53).

These let the chat agent turn a completed Analysis into a dashboard: a set of
chart/widget specs the separate frontend renders. One analysis can have many
dashboards.

The agent describes widgets (type + which analysis columns they bind to +
aggregation + title); the backend VALIDATES every widget against an allowlist of
widget/aggregation types and confirms each referenced column_id actually belongs
to the analysis before persisting. The model never supplies SQL, table names, or
raw column names — only ids verified against the analysis's own columns.

Use analysis_get first to discover the analysis's column ids/types, then build
widgets that reference those column_ids.

All DB access is delegated to dashboard_store (table name is a module constant
there). Tools are async; the sync httpx DB calls are wrapped in asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from langchain_core.tools import tool

import dashboard_store as store


def _ok(payload: dict) -> str:
    return json.dumps(payload, indent=2, default=str)


def _err(msg: str) -> str:
    return json.dumps({"error": msg}, indent=2, default=str)


_WIDGET_HELP = (
    "Each widget is an object: {id, type, title, encoding|columns, layout?, options?}. "
    f"type ∈ {sorted(store.WIDGET_TYPES)}. "
    "For bar/line/area/scatter set encoding.x and encoding.y; for pie/kpi set "
    "encoding.value (and pie usually encoding.series or encoding.group_by); for a "
    "table set a 'columns' list of analysis column_ids. Each encoding channel is "
    "{column_id, aggregation?} where aggregation ∈ "
    f"{sorted(store.AGGREGATIONS)}. column_ids MUST come from this analysis "
    "(call analysis_get to list them)."
)


@tool
async def create_dashboard(analysis_id: str, title: str, widgets: list,
                           description: str = "") -> str:
    """Create a dashboard for an analysis from a list of chart widgets.

    Build a dashboard view over a completed analysis. {help}

    Args:
        analysis_id: The analysis this dashboard visualises.
        title: Short human title for the dashboard.
        widgets: List of widget objects (see the description above).
        description: Optional longer description.
    """
    try:
        spec = {"widgets": widgets or []}
        d = await asyncio.to_thread(
            store.create_dashboard, analysis_id, title, spec,
            description=description or None)
        return _ok({"dashboard_id": d["id"], "analysis_id": d["analysis_id"],
                    "title": d["title"], "widgets": len(d["spec"].get("widgets", [])),
                    "spec": d["spec"]})
    except store.DashboardNotFound as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


create_dashboard.description = create_dashboard.description.replace("{help}", _WIDGET_HELP)


@tool
async def suggest_dashboard(analysis_id: str, max_widgets: int = 8,
                            persist: bool = False, title: str = "") -> str:
    """Auto-suggest a sensible STARTER dashboard spec for a completed analysis,
    so you don't have to hand-build every widget. Inspects the analysis's columns
    (sniffing each data column's values for numeric vs categorical) and proposes:
    a KPI (sum) per numeric column, a bar chart of the first numeric grouped by
    the first categorical, and a table of the primary columns.

    The returned spec is already validated, so you can present it to the user and
    then call create_dashboard / update_dashboard with their edits. By default it
    is NOT persisted; pass persist=true to save it as a new dashboard immediately.

    Args:
        analysis_id: The analysis to suggest a dashboard for.
        max_widgets: Cap on suggested widgets (default 8).
        persist: If true, save the suggestion as a new dashboard and return its id.
        title: Optional dashboard title (used only when persist=true; defaults to
               the suggested title).
    """
    try:
        spec = await asyncio.to_thread(store.suggest_spec, analysis_id, max_widgets)
        payload: dict[str, Any] = {
            "analysis_id": analysis_id,
            "persisted": False,
            "suggested_title": spec.get("title"),
            "widgets": len(spec.get("widgets", [])),
            "spec": spec,
        }
        if persist:
            d = await asyncio.to_thread(
                store.create_dashboard, analysis_id,
                title or spec.get("title") or "Starter dashboard", spec)
            payload["persisted"] = True
            payload["dashboard_id"] = d["id"]
            payload["title"] = d["title"]
        return _ok(payload)
    except store.DashboardNotFound as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@tool
async def list_dashboards(analysis_id: str) -> str:
    """List the dashboards that belong to an analysis (with their full specs).

    Args:
        analysis_id: The analysis whose dashboards to list.
    """
    try:
        rows = await asyncio.to_thread(store.list_dashboards, analysis_id)
        return _ok({"count": len(rows), "dashboards": [
            {"dashboard_id": r["id"], "title": r["title"],
             "description": r.get("description"),
             "widgets": len((r.get("spec") or {}).get("widgets", [])),
             "spec": r.get("spec")} for r in rows]})
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@tool
async def update_dashboard(dashboard_id: str, title: str = "",
                           description: str = "", widgets: Optional[list] = None) -> str:
    """Update a dashboard's title, description, or widgets. Only non-empty
    arguments are applied; passing widgets replaces the whole widget list.

    Args:
        dashboard_id: The dashboard to update.
        title: New title (optional).
        description: New description (optional).
        widgets: New full list of widget objects (optional — replaces all widgets).
    """
    try:
        patch: dict[str, Any] = {}
        if title:
            patch["title"] = title
        if description:
            patch["description"] = description
        if widgets is not None:
            patch["spec"] = {"widgets": widgets}
        if not patch:
            return _err("Nothing to update — provide title, description, or widgets.")
        d = await asyncio.to_thread(store.update_dashboard, dashboard_id, patch)
        return _ok({"dashboard_id": dashboard_id, "updated": list(patch.keys()),
                    "title": (d or {}).get("title"), "spec": (d or {}).get("spec")})
    except store.DashboardNotFound as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@tool
async def delete_dashboard(dashboard_id: str) -> str:
    """Delete a dashboard.

    Args:
        dashboard_id: The dashboard to delete.
    """
    try:
        await asyncio.to_thread(store.delete_dashboard, dashboard_id)
        return _ok({"deleted_dashboard_id": dashboard_id})
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")
