"""dashboard_store.py — data-access + spec validation for analysis dashboards
(Task #53).

A dashboard turns a completed analysis into a set of chart/widget specs that the
separate Vercel frontend renders. One analysis can have many dashboards.

Design (mirrors analysis_store.py):
  - The table name is a MODULE CONSTANT (T_DASHBOARDS), never taken from the
    model/caller. There is no generic "run arbitrary SQL" path here.
  - The dashboard `spec` is validated against an ALLOWLIST of widget + aggregation
    types BEFORE it is persisted. Widgets bind to analysis columns by `column_id`
    only; every referenced column_id must actually belong to the analysis. The
    model never supplies raw SQL, table names, or column names — just ids that we
    verify against the analysis's own columns.

Low-level REST helpers are reused from analysis_store (service-role httpx layer)
to avoid divergence. All functions are synchronous; async callers should wrap
them in asyncio.to_thread.
"""
from __future__ import annotations

from typing import Any, Optional

import analysis_store as _store
from analysis_store import _delete, _first, _insert, _now, _patch, _select  # reuse REST layer

# Hard-scoped table name — a constant, never supplied by the model.
T_DASHBOARDS = "dashboards"

SPEC_VERSION = 1
MAX_WIDGETS = 50

# Allowlists. The model picks from these by short key; anything else is rejected.
WIDGET_TYPES = {"bar", "line", "area", "pie", "scatter", "kpi", "table"}
AGGREGATIONS = {"sum", "avg", "count", "min", "max", "median", "none"}

# Encoding channels a widget may bind to analysis columns through.
CHANNELS = {"x", "y", "series", "group_by", "value"}

# Per-type minimum required channels (the frontend renderer relies on these).
_REQUIRED_CHANNELS = {
    "bar": ("x", "y"),
    "line": ("x", "y"),
    "area": ("x", "y"),
    "scatter": ("x", "y"),
    "pie": ("value",),
    "kpi": ("value",),
    # "table" is validated separately (it uses a `columns` list, not channels).
}


class DashboardError(Exception):
    """Configuration / validation / REST failure (maps to HTTP 400/500)."""


class DashboardNotFound(Exception):
    """Target dashboard / analysis does not exist (maps to HTTP 404)."""


# ---------- spec validation ----------

def _err(msg: str) -> "DashboardError":
    return DashboardError(msg)


def _validate_binding(channel: str, binding: Any, valid_column_ids: set, where: str) -> dict:
    if not isinstance(binding, dict):
        raise _err(f"{where}: channel '{channel}' must be an object like "
                   f"{{'column_id': '…', 'aggregation': 'sum'}}.")
    column_id = binding.get("column_id")
    if not column_id or not isinstance(column_id, str):
        raise _err(f"{where}: channel '{channel}' requires a string 'column_id'.")
    if column_id not in valid_column_ids:
        raise _err(f"{where}: channel '{channel}' references column_id "
                   f"'{column_id}', which is not a column of this analysis.")
    out = {"column_id": column_id}
    agg = binding.get("aggregation")
    if agg is not None:
        if agg not in AGGREGATIONS:
            raise _err(f"{where}: aggregation '{agg}' is not allowed. "
                       f"Use one of {sorted(AGGREGATIONS)}.")
        out["aggregation"] = agg
    return out


def _validate_layout(layout: Any, where: str) -> Optional[dict]:
    if layout is None:
        return None
    if not isinstance(layout, dict):
        raise _err(f"{where}: layout must be an object with integer x, y, w, h.")
    out = {}
    for k in ("x", "y", "w", "h"):
        if k in layout and layout[k] is not None:
            try:
                v = int(layout[k])
            except (TypeError, ValueError):
                raise _err(f"{where}: layout.{k} must be an integer.")
            if v < 0:
                raise _err(f"{where}: layout.{k} must be >= 0.")
            out[k] = v
    return out or None


def _validate_widget(idx: int, widget: Any, valid_column_ids: set, seen_ids: set) -> dict:
    where = f"widgets[{idx}]"
    if not isinstance(widget, dict):
        raise _err(f"{where}: must be an object.")

    wid = widget.get("id")
    if not wid or not isinstance(wid, str):
        raise _err(f"{where}: requires a non-empty string 'id'.")
    if wid in seen_ids:
        raise _err(f"{where}: duplicate widget id '{wid}'.")
    seen_ids.add(wid)

    wtype = widget.get("type")
    if wtype not in WIDGET_TYPES:
        raise _err(f"{where}: type '{wtype}' is not allowed. "
                   f"Use one of {sorted(WIDGET_TYPES)}.")

    title = widget.get("title", "")
    if title is not None and not isinstance(title, str):
        raise _err(f"{where}: title must be a string.")

    norm: dict[str, Any] = {"id": wid, "type": wtype, "title": (title or "")}

    if wtype == "table":
        cols = widget.get("columns")
        if not isinstance(cols, list) or not cols:
            raise _err(f"{where}: a 'table' widget requires a non-empty "
                       f"'columns' list of analysis column_ids.")
        norm_cols = []
        for cid in cols:
            if cid not in valid_column_ids:
                raise _err(f"{where}: columns references column_id '{cid}', "
                           f"which is not a column of this analysis.")
            norm_cols.append(cid)
        norm["columns"] = norm_cols
    else:
        encoding = widget.get("encoding") or {}
        if not isinstance(encoding, dict):
            raise _err(f"{where}: encoding must be an object of channel→binding.")
        norm_enc: dict[str, Any] = {}
        for channel, binding in encoding.items():
            if channel not in CHANNELS:
                raise _err(f"{where}: channel '{channel}' is not allowed. "
                           f"Use one of {sorted(CHANNELS)}.")
            norm_enc[channel] = _validate_binding(channel, binding, valid_column_ids, where)
        required = _REQUIRED_CHANNELS.get(wtype, ())
        missing = [c for c in required if c not in norm_enc]
        if missing:
            raise _err(f"{where}: a '{wtype}' widget requires channel(s) "
                       f"{missing} in its encoding.")
        norm["encoding"] = norm_enc

    layout = _validate_layout(widget.get("layout"), where)
    if layout:
        norm["layout"] = layout

    options = widget.get("options")
    if options is not None:
        if not isinstance(options, dict):
            raise _err(f"{where}: options must be an object.")
        norm["options"] = options

    return norm


def validate_spec(spec: Any, valid_column_ids: set) -> dict:
    """Validate + normalize a dashboard spec against the analysis's columns.

    Returns a normalized spec ({"version", "widgets": [...]}) containing only
    allowlisted keys. Raises DashboardError on any violation.
    """
    if not isinstance(spec, dict):
        raise _err("spec must be an object like {'widgets': [...]}.")
    widgets = spec.get("widgets")
    if not isinstance(widgets, list):
        raise _err("spec.widgets must be a list of widget objects.")
    if len(widgets) > MAX_WIDGETS:
        raise _err(f"too many widgets ({len(widgets)}); max is {MAX_WIDGETS}.")

    seen_ids: set = set()
    norm_widgets = [
        _validate_widget(i, w, valid_column_ids, seen_ids) for i, w in enumerate(widgets)
    ]
    out: dict[str, Any] = {"version": SPEC_VERSION, "widgets": norm_widgets}
    title = spec.get("title")
    if isinstance(title, str) and title:
        out["title"] = title
    layout_mode = spec.get("layout")
    if isinstance(layout_mode, str) and layout_mode:
        out["layout"] = layout_mode
    return out


def _valid_column_ids(analysis_id: str) -> set:
    return {c["id"] for c in _store.list_columns(analysis_id)}


# ---------- starter-dashboard suggestion ----------

# Heuristic knobs for suggest_spec.
SUGGEST_MAX_WIDGETS = 8          # default cap on a suggested starter dashboard
_SUGGEST_SAMPLE = 25             # cells sampled per column when sniffing types
_NUMERIC_THRESHOLD = 0.6         # fraction of non-empty samples that must parse as numbers
_TABLE_MAX_COLS = 6              # columns shown in the suggested table widget


def _looks_numeric(value: Any) -> bool:
    """True if a cell value parses as a number (tolerating $, %, and thousands separators)."""
    if value is None:
        return False
    s = str(value).strip()
    if not s:
        return False
    s = s.replace(",", "").lstrip("$").rstrip("%").strip()
    if not s:
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


def _classify_columns(columns: list, cells: list) -> dict:
    """Split data columns into numeric vs categorical by sniffing their cell values.

    Returns {"numeric": [...], "categorical": [...], "ai": [...]} lists of column
    dicts, preserving the analysis's column order. AI columns are reported
    separately (they hold free text and are never used as KPI/axis candidates,
    only offered in the table widget).
    """
    by_col: dict[str, list] = {}
    for c in cells:
        by_col.setdefault(c.get("column_id"), []).append(c.get("value"))

    numeric, categorical, ai = [], [], []
    for col in columns:
        if col.get("type") == "ai":
            ai.append(col)
            continue
        values = [v for v in by_col.get(col["id"], []) if v not in (None, "")]
        sample = values[:_SUGGEST_SAMPLE]
        if sample:
            hits = sum(1 for v in sample if _looks_numeric(v))
            if hits / len(sample) >= _NUMERIC_THRESHOLD:
                numeric.append(col)
                continue
        categorical.append(col)
    return {"numeric": numeric, "categorical": categorical, "ai": ai}


def _title_case(name: str) -> str:
    return (name or "").strip() or "value"


def suggest_spec(analysis_id: str, max_widgets: Optional[int] = None) -> dict:
    """Build a *valid* starter dashboard spec from a completed analysis's columns.

    Heuristics (column types are sniffed from each data column's cell values):
      - one KPI (sum) per numeric column,
      - a bar chart of the first numeric column (sum) grouped by the first
        categorical column (x = categorical, y = numeric/sum),
      - a table of the primary columns.

    The returned spec is run through validate_spec, so it is always safe to hand
    straight to create_dashboard / update_dashboard. Nothing is persisted here.
    Raises DashboardNotFound if the analysis does not exist.
    """
    analysis = _store.get_analysis(analysis_id)
    if not analysis:
        raise DashboardNotFound(f"No analysis with id '{analysis_id}'.")

    columns = _store.list_columns(analysis_id)
    if not columns:
        raise _err("This analysis has no columns yet — add data/AI columns first.")
    cells = _store.list_cells(analysis_id)

    groups = _classify_columns(columns, cells)
    numeric = groups["numeric"]
    categorical = groups["categorical"]

    cap = SUGGEST_MAX_WIDGETS if max_widgets is None else int(max_widgets)
    cap = max(1, min(cap, MAX_WIDGETS))

    widgets: list[dict] = []

    def _add(widget: dict) -> bool:
        if len(widgets) >= cap:
            return False
        widgets.append(widget)
        return True

    # 1) KPI (sum) per numeric column.
    for i, col in enumerate(numeric):
        if not _add({
            "id": f"kpi_{col['id']}",
            "type": "kpi",
            "title": f"Total {_title_case(col['name'])}",
            "encoding": {"value": {"column_id": col["id"], "aggregation": "sum"}},
        }):
            break

    # 2) Bar chart: first numeric (sum) grouped by first categorical.
    if numeric and categorical:
        cat = categorical[0]
        num = numeric[0]
        _add({
            "id": f"bar_{num['id']}_by_{cat['id']}",
            "type": "bar",
            "title": f"{_title_case(num['name'])} by {_title_case(cat['name'])}",
            "encoding": {
                "x": {"column_id": cat["id"]},
                "y": {"column_id": num["id"], "aggregation": "sum"},
            },
        })

    # 3) Table of the primary columns (prefer categorical first, then numeric).
    primary = (categorical + numeric)[:_TABLE_MAX_COLS]
    if not primary:
        primary = [c for c in columns][:_TABLE_MAX_COLS]
    if primary:
        _add({
            "id": "table_primary",
            "type": "table",
            "title": "Key columns",
            "columns": [c["id"] for c in primary],
        })

    spec = {
        "title": f"{analysis.get('title') or 'Analysis'} — starter dashboard",
        "widgets": widgets,
    }
    # Validate against the analysis's own columns before returning.
    return validate_spec(spec, _valid_column_ids(analysis_id))


# ---------- CRUD ----------

def create_dashboard(analysis_id: str, title: str, spec: dict, *,
                     description: Optional[str] = None,
                     created_by: Optional[str] = None) -> dict:
    analysis = _store.get_analysis(analysis_id)
    if not analysis:
        raise DashboardNotFound(f"No analysis with id '{analysis_id}'.")
    normalized = validate_spec(spec or {"widgets": []}, _valid_column_ids(analysis_id))
    row = {
        "analysis_id": analysis_id,
        # project scope ALWAYS follows the parent analysis — never a client value,
        # so a dashboard can't be silently attached to a different project.
        "project_id": analysis.get("project_id"),
        "title": (title or "Untitled dashboard").strip() or "Untitled dashboard",
        "description": description,
        "spec": normalized,
        "created_by": created_by,
    }
    return _first(_insert(T_DASHBOARDS, row))


def get_dashboard(dashboard_id: str) -> Optional[dict]:
    return _first(_select(T_DASHBOARDS, filters=[f"id=eq.{dashboard_id}"], limit=1))


def list_dashboards(analysis_id: str, *, limit: int = 100) -> list:
    return _select(T_DASHBOARDS, filters=[f"analysis_id=eq.{analysis_id}"],
                   order="updated_at.desc", limit=max(1, min(int(limit or 100), 200)))


def update_dashboard(dashboard_id: str, patch: dict) -> Optional[dict]:
    existing = get_dashboard(dashboard_id)
    if not existing:
        raise DashboardNotFound(f"No dashboard with id '{dashboard_id}'.")
    clean: dict[str, Any] = {}
    if "title" in patch and patch["title"] is not None:
        clean["title"] = str(patch["title"]).strip() or "Untitled dashboard"
    if "description" in patch:
        clean["description"] = patch["description"]
    if "spec" in patch:
        clean["spec"] = validate_spec(patch["spec"] or {"widgets": []},
                                      _valid_column_ids(existing["analysis_id"]))
    if not clean:
        raise DashboardError("nothing to update — provide title, description, or spec.")
    clean["updated_at"] = _now()
    return _first(_patch(T_DASHBOARDS, {"id": dashboard_id}, clean))


def delete_dashboard(dashboard_id: str) -> None:
    if not get_dashboard(dashboard_id):
        raise DashboardNotFound(f"No dashboard with id '{dashboard_id}'.")
    _delete(T_DASHBOARDS, {"id": dashboard_id})
