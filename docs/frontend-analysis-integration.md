# Frontend ↔ Analysis backend integration contract

The Analysis frontend is a **separate Vercel project**. It uses two channels:

1. **Reads (live):** Supabase realtime + `anon` SELECT directly on the `analysis_*`
   tables. No backend round-trip for live cell updates.
2. **Writes / actions:** the **Bearer-gated** REST API below (service-role store
   runs the writes server-side).

## Auth model

- All `/api/analysis…` endpoints require `Authorization: Bearer <token>`
  (same token gate as the rest of the non-`/mcp` HTTP API). Without it → **401**.
- The browser reads tables with the Supabase **anon** key.
- **By design, drive all writes through the Bearer-gated REST API**, not the anon
  key. Note this is an *application* convention, not a DB-enforced wall: consistent
  with the project-wide Supabase posture (RLS **disabled** on all tables, grants
  left at Supabase defaults — see [security-auth.md](security-auth.md)), the
  `analysis_*` tables are not write-locked at the database layer. Do **not** rely on
  the anon key being unable to write, and do **not** `REVOKE` grants (a prior revoke
  broke the live frontend). If real DB-level write protection is ever needed, the
  project's documented path is an explicit scoped RLS policy, not a grant change.

## Realtime reads

Subscribe to Postgres changes on these tables, filtered by `analysis_id`:

- `analysis_cells` — the main live feed (`status`, `value`, `model_used`,
  `tokens_used`). Has replica identity full → UPDATE payloads carry the full row.
- `analysis_rows`, `analysis_columns` — structure changes.
- `analysis_runs` — run progress (`cells_done` / `cells_error` / `status`).

Cell `status` lifecycle: `empty → pending → running → done | error`.
Run `status`: `running → done | error | stopped`.

Recommended initial load: `GET /api/analysis/{id}` (full snapshot) once, then
apply realtime deltas on top.

## REST endpoints

Base path `/api/analysis`. JSON in/out; errors are `{ "error": "…" }` with a 4xx/5xx code.

| Method & path | Body | Returns |
| --- | --- | --- |
| `GET /api/analysis/models` | — | `{ default, models[], providers[] }` |
| `POST /api/analysis` | `{ title, description?, project_id?, chat_id?, created_by?, source_config? }` | created analysis row |
| `GET /api/analysis?project_id=&chat_id=&limit=` | — | `{ count, analyses[] }` |
| `GET /api/analysis/{id}` | — | full snapshot `{ analysis, columns[], rows[], cells[], runs[], latest_run }` (404 if missing) |
| `PATCH /api/analysis/{id}` | any of `title, description, status, source_config` | updated analysis |
| `DELETE /api/analysis/{id}` | — | `{ deleted }` (cascades columns/rows/cells/runs) |
| `POST /api/analysis/{id}/columns` | `{ name, type: "data"\|"ai", config, position? }` | created column (data cols auto-populate cells) |
| `PATCH /api/analysis/columns/{column_id}` | any of `name, position, config` | updated column |
| `DELETE /api/analysis/columns/{column_id}` | — | `{ deleted_column }` |
| `POST /api/analysis/{id}/rows` | source form **or** explicit form (below) | source: `{ source, found, added }`; explicit: `{ added, rows[] }` |
| `DELETE /api/analysis/rows/{row_id}` | — | `{ deleted_row }` |
| `PATCH /api/analysis/cells/{cell_id}` | `{ value }` | updated cell (manual edit → status `done`, `model_used: "manual"`) |
| `POST /api/analysis/{id}/cells/rerun` | `{ cell_id }` **or** `{ row_id, column_id }` | re-run one AI cell `{ status, row_id, column_id, value?/error? }` (404 unknown target; 400 non-AI / missing ids; 409 if a full run or another re-run is active) |
| `POST /api/analysis/{id}/run` | — | `{ status: "started", analysis_id }` (409 if a run is active) |
| `POST /api/analysis/{id}/stop` | — | `{ status, analysis_id }` |
| `GET /api/analysis/{id}/runs?limit=` | — | `{ is_running, count, runs[] }` |
| `POST /api/analysis/{id}/query` | `{ question, model? }` | `{ answer, … }` (read-only) |

### Column `config` shapes

- **data:** `{ "source_field": "amount" }` — copies `row.source[source_field]`.
- **ai:** `{ "system_prompt", "model": "provider:model", "instructions"?, "input_columns"?: [column_id, …] }`.
  `model` is validated against supported providers; `input_columns` lets a column
  read earlier columns' cell values for the same row.

### Adding rows — two forms

**From a cache source** (recommended):

```json
{ "source": "opportunity_cache",
  "limit": 25,
  "stage": "…", "momentum": "…",
  "min_amount": 0, "max_amount": 0,
  "account_contains": "…", "name_contains": "…", "is_closed": false }
```

`source` is `"opportunity_cache"` (full SF mirror) or `"opportunity_observatory"`
(header-only). All filters optional.

**Explicit rows:**

```json
{ "rows": [ { "entity_ref": "006…", "label": "Acme – Renewal", "source": { "amount": 120000, … } } ] }
```

## Dashboards (spec-driven chart views)

A **dashboard** turns a completed analysis into a set of chart/widget specs the
frontend renders in the right panel's "dashboard view" (toggled from the sheet
view). One analysis can have **many** dashboards, each independently retrievable.

The agent (or the frontend) supplies a `spec`; the backend **validates** it
against an allowlist of widget + aggregation types and confirms every referenced
`column_id` actually belongs to the analysis **before** persisting. The model
never supplies SQL, table names, or raw column names — only column_ids that are
verified against the analysis's own columns.

### Dashboard endpoints

| Method & path | Body | Returns |
| --- | --- | --- |
| `POST /api/analysis/{id}/dashboards` | `{ title, widgets[] }` (or `{ title, spec }`), `description?`, `created_by?` | created dashboard row (404 if analysis missing; 400 on spec violation). `project_id` is **inherited from the parent analysis** (any client value is ignored). |
| `POST /api/analysis/{id}/dashboards/suggest` | `{ max_widgets?, persist?, title?, description? }` (all optional; empty body OK) | auto-suggested **starter** spec from the analysis's columns. Default `{ persisted:false, analysis_id, spec }` (a validated draft, not saved). With `persist:true`: `{ persisted:true, dashboard }` (saved as a new dashboard). 404 if analysis missing; 400 if it has no columns. |
| `GET /api/analysis/{id}/dashboards?limit=` | — | `{ count, dashboards[] }` (newest first; each has the full `spec`) |
| `GET /api/analysis/dashboards/{dashboard_id}` | — | dashboard row (404 if missing) |
| `PATCH /api/analysis/dashboards/{dashboard_id}` | any of `title, description, spec` (or `widgets[]`) | updated dashboard (404 missing; 400 on spec violation) |
| `DELETE /api/analysis/dashboards/{dashboard_id}` | — | `{ deleted_dashboard }` |

`widgets[]` is a convenience: the backend wraps it into `spec = { widgets }`.
Pass either `widgets` or a full `spec` object — `spec` wins if both are given.

### Dashboard spec shape (stable contract)

The stored `spec` is always normalized to:

```json
{
  "version": 1,
  "title": "optional",
  "layout": "optional layout-mode string",
  "widgets": [
    {
      "id": "w1",
      "type": "bar",
      "title": "Amount by stage",
      "encoding": {
        "x": { "column_id": "<analysis column id>" },
        "y": { "column_id": "<analysis column id>", "aggregation": "sum" }
      },
      "layout": { "x": 0, "y": 0, "w": 6, "h": 4 },
      "options": { }
    }
  ]
}
```

- **Widget `type`** ∈ `bar | line | area | scatter | pie | kpi | table`.
- **`encoding`** maps a channel to a column binding. Channels ∈
  `x | y | series | group_by | value`. Each binding is
  `{ "column_id", "aggregation"? }`; `aggregation` ∈
  `sum | avg | count | min | max | median | none`.
- **Required channels per type** (enforced):
  `bar/line/area/scatter` → `x` + `y`; `pie`/`kpi` → `value`
  (pie usually also `series` or `group_by` for the category).
- **`table`** widgets use `"columns": ["<column_id>", …]` instead of `encoding`.
- **`layout`** (optional per widget) is `{ x, y, w, h }`, non-negative ints — a
  grid placement hint for the renderer.
- **`options`** (optional) is a free-form object for display hints (colors,
  stacked, legend, …); the backend stores it verbatim without interpreting it.
- Widget `id`s must be unique within a dashboard. Max **50** widgets per spec.

Invalid specs (unknown type/aggregation/channel, a `column_id` not in the
analysis, a missing required channel, duplicate ids, >50 widgets) are rejected
with **400** and a message naming the offending `widgets[i]`.

### Realtime + rendering the dashboard view

- `public.dashboards` has **replica identity full** and is in the
  `supabase_realtime` publication, with `anon`/`authenticated` `SELECT` — the
  browser can read + subscribe just like the other `analysis_*` tables.
- Dashboard-view load: `GET /api/analysis/{id}/dashboards` to list, then render
  each widget by reading the analysis's cells (already loaded from
  `GET /api/analysis/{id}`) for the `column_id`s the widgets bind to and applying
  the widget's `aggregation` client-side. No extra data endpoint is needed — the
  spec is purely a description of how to chart columns the frontend already has.

## Notes for the frontend

- Treat `analysis_runs` as the source of truth for progress; don't infer
  completion from cell counts alone.
- `Run All` is idempotent per analysis: a second `POST …/run` while one is active
  returns **409** — surface "already running" instead of erroring out.
- `query` never mutates state; safe to call any time.
- Each chat turn / run still obeys the one-terminal-row contract elsewhere in the
  app; the analysis run lifecycle is tracked in `analysis_runs`, separate from chat.
