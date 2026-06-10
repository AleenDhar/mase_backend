# Analysis feature (backend)

Spreadsheet-style analyses over many Salesforce opportunities. **Rows** are
opportunities (sourced from the local `opportunity_cache` / `opportunity_observatory`
mirrors — no live SOQL). **Columns** are either:

- **data columns** — copy a field straight out of the row's `source` JSON, or
- **AI columns** — each has its own `system_prompt` + `model`; a "Run All" fills
  every AI cell row-by-row, left-to-right.

After cells are filled the user can ask read-only questions over the table.

The frontend is a separate Vercel project. It reads live updates straight from
Supabase realtime and drives writes through the Bearer-gated REST API documented
in [frontend-analysis-integration.md](frontend-analysis-integration.md).

## Components

| File | Role |
| --- | --- |
| `migrations/0002_analysis.sql` | Canonical DDL (documentation + source of truth for the schema). |
| `scripts/setup_analysis_schema.py` | Applies the DDL via the Supabase Management API and (idempotently) adds each table to the `supabase_realtime` publication. Re-runnable. |
| `analysis_store.py` | Functional httpx REST data layer (service-role key). Hard-scoped table-name constants; all CRUD + sourcing + cell population + `get_full_analysis`. **Sync** — endpoints wrap it in a thread. |
| `analysis_engine.py` | Run-All engine, in-process run registry, model resolver, read-only `query_analysis`. |
| `custom_tools/analysis_tools.py` | Async `@tool`s so the chat agent can build/run/query analyses. Auto-loaded by `CustomToolsLoader`. |
| `migrations/0003_dashboards.sql` + `scripts/setup_dashboards_schema.py` | Dashboards table DDL + idempotent setup (realtime + grants). |
| `dashboard_store.py` | Dashboard CRUD + `validate_spec` (widget/aggregation allowlist; column_id binding check). Reuses analysis_store's REST layer. |
| `custom_tools/dashboard_tools.py` | Async `@tool`s (`create_dashboard`, `list_dashboards`, `update_dashboard`, `delete_dashboard`). Auto-loaded. |
| REST endpoints in `server.py` | `/api/analysis…` + `/api/analysis/{id}/dashboards` + `/api/analysis/dashboards/{id}` (see the frontend doc). |

## Schema (5 tables, all `public`)

- **analyses** — `id, project_id, chat_id, title, description, status (draft|running|done|error), source_config jsonb, created_by, created_at, updated_at`.
- **analysis_columns** — `id, analysis_id, name, position, type (data|ai), config jsonb, …`.
  - data `config`: `{ "source_field": "amount" }`.
  - ai `config`: `{ "system_prompt", "model", "instructions"?, "input_columns"?: [column_id…], "use_tools"?: bool }`.
    `use_tools` defaults **true** — the cell agent gets the **same tool catalog as the
    main chat agent** (custom tools + all MCP servers, denylist already applied). Set
    `use_tools: false` for a cheaper/faster single-shot completion with no tools.
- **analysis_rows** — `id, analysis_id, position, entity_ref (e.g. SF opportunity id), label, source jsonb`.
- **analysis_cells** — `id, analysis_id, row_id, column_id, value, status (empty|pending|running|done|error), error, model_used, tokens_used, updated_at`, `unique(row_id, column_id)`.
- **analysis_runs** — `id, analysis_id, status (running|done|error|stopped), cells_total, cells_done, cells_error, error, started_at, finished_at`.

`analysis_cells`, `analysis_rows`, `analysis_runs` have **replica identity full** so
realtime UPDATE payloads carry complete rows. RLS is left **disabled** and grants stay
at Supabase defaults (matches the project-wide posture in
[security-auth.md](security-auth.md)). The migration adds an explicit `GRANT SELECT`
for clarity, but does **not** revoke write grants (a prior revoke broke the live
frontend). Writes are routed through the service-role store as an *application*
convention, not a DB-enforced wall — see
[frontend-analysis-integration.md](frontend-analysis-integration.md).

## Run-All engine

`start_run(analysis_id)` registers an in-process run and kicks off a background task;
it raises `AnalysisRunError` (→ HTTP 409) if a run is already active for that
analysis. Per run:

- Rows are processed with **bounded parallelism** (`ANALYSIS_ROW_CONCURRENCY`, default 4).
- Within a row, **AI columns run sequentially left-to-right** so later columns can
  reference earlier ones (via `config.input_columns`).
- Each cell transitions `pending → running → done|error`; `model_used` and
  `tokens_used` are recorded. The owning `analysis_runs` row tracks
  `cells_done` / `cells_error` and a terminal `status`.
- **Tool use:** unless `config.use_tools` is `false`, each AI cell runs as a bounded
  `create_react_agent` over the shared tool catalog (same tools as the main chat
  agent; provider wired at startup via `analysis_engine.set_tool_provider`). The
  tool loop is bounded by `ANALYSIS_CELL_TOOL_RECURSION` (default 12); if the loop
  errors (e.g. recursion limit), the cell **falls back** to a plain single-shot
  completion so the run keeps going. `tokens_used` sums usage across the agent's
  messages. Cells inherit any future `MCP_TOOL_DENYLIST` automatically.
- `stop_run(analysis_id)` sets the stop event; in-flight cells finish, the rest are
  left as-is and the run ends `stopped`.

Data columns don't need the engine — `populate_data_cells` fills them synchronously
from each row's `source` whenever rows/columns change.

### Single-cell edit & re-run

- **Manual edit:** `store.edit_cell(cell_id, value)` (REST `PATCH /api/analysis/cells/{cell_id}`,
  tool `analysis_edit_cell`) overrides a cell's value, marks it `done`, and records
  `model_used = "manual"`.
- **Single-cell re-run:** `engine.rerun_cell(analysis_id, cell_id=… | row_id=…, column_id=…)`
  (REST `POST /api/analysis/{id}/cells/rerun`, tool `analysis_rerun_cell`) re-runs one
  AI cell using its data columns + earlier AI columns as inputs. AI cells only.
  - **Concurrency:** Run-All and single-cell re-run are mutually exclusive per analysis.
    `start_run` and `rerun_cell` share a `_rerun_active` claim that is checked-and-set
    with no `await` in between, which is atomic under asyncio's single-event-loop
    scheduling — so a concurrent `/run` + `/cells/rerun` deterministically yields one
    success and one `409`. This guard is **process-local**; running multiple server
    workers/processes would need a distributed lock (DB/Redis) to keep it.
  - **Errors:** `404` unknown cell/row/column, `400` non-AI target or missing ids,
    `409` if a full run or another re-run is already active.

### Resume an interrupted run

- **Resume:** `engine.start_resume(analysis_id)` (REST `POST /api/analysis/{id}/resume`)
  relaunches a Run-All in **resume mode**: AI cells already marked `done` keep their
  values (and still feed columns to their right), and only the remaining
  `pending`/`running`/`error` cells are recomputed with the normal row-parallel,
  left-to-right semantics. Used to finish a run that was interrupted (e.g. a server
  restart left `analyses.status = "running"` frozen). The run record's `cells_done`
  resumes at the already-done count, and `analyses.status` is reset to `done`/`error`
  when finished. Same per-analysis concurrency guard as `start_run` (`409` if a run or
  single-cell re-run is already active).

### Models

`validate_model("provider:model")` is enforced for AI columns. Supported providers:
`anthropic`, `openai`, `google` / `google_genai`, `xai`. Default model comes from
`ANALYSIS_DEFAULT_MODEL` → `MODEL` → `anthropic:claude-sonnet-4-6-20260901`.
`GET /api/analysis/models` returns the curated `MODEL_SUGGESTIONS` plus the default
and provider list (any valid `provider:model` is accepted, not just the suggestions).

### query_analysis

`query_analysis(analysis_id, question, model=None)` renders the full table (columns
+ rows + cell values) into a compact context and asks a single LLM call to answer
**read-only** — it never mutates the analysis.

### Structured fetch / filter / search tools

Read-only, deterministic alternatives to dumping the grid to an LLM — and the
reason the agent should NOT reach for the generic `supabase_query` to introspect
analysis cells (`analysis_get` only previews 5 rows with no values; `query_analysis`
truncates by position). All three live in `custom_tools/analysis_tools.py`, backed
by hard-table-scoped reads in `analysis_store.py`, and page their results.

- **`analysis_filter_rows(analysis_id, column_name, op, value, row_label_contains,
  data_only, include_columns, page, page_size)`** — find rows matching a column
  predicate and/or an opportunity-name (`row_label_contains`) match, returning each
  row's cells. `op` ∈ `empty|not_empty|equals|contains|gt|gte|lt|lte` (gt/gte/lt/lte
  compare numerically; equals/contains case-insensitive). `data_only=true` omits the
  verbose AI columns; `include_columns` returns only the named columns. The value/
  label matching runs in Python, so those args are not a filter-string vector.
- **`analysis_search_cells(analysis_id, query, column_name, data_only, page,
  page_size)`** — case-insensitive substring search across cell values (`ilike`
  pushed to the DB; the model term is URL-encoded). Optionally restricted to one
  column and/or data columns. Returns matches with row label, column, and a snippet.
- **`analysis_get_cells(cell_ids)`** — full untruncated values for specific cells
  (the other two return 300-char snippets + the `cell_id` for drill-down). Ids are
  UUID-validated and fetched in ≤100-id chunks.

Pagination: each paginated response carries a `pagination` block (`page`,
`page_size`, `total`, `total_pages`, `has_more`, `next_page`) plus a `hint` telling
the agent to fetch the next page while `has_more` is true. `analysis_search_cells`
also surfaces a `capped` flag if the match set hits the server cap (2000).

## Dashboards (Task #53)

A dashboard turns a completed analysis into chart/widget specs the frontend
renders (right-panel "dashboard view"). One analysis → many dashboards.

- **dashboards** — `id, analysis_id (FK→analyses, on delete cascade), project_id, title, description, spec jsonb, created_by, created_at, updated_at`. `replica identity full`; in `supabase_realtime`; `GRANT SELECT` to anon/authenticated (same posture as the `analysis_*` tables).
- **`spec` validation (`dashboard_store.validate_spec`)** — the spec is checked against an allowlist BEFORE persist: widget `type` ∈ `bar|line|area|scatter|pie|kpi|table`; encoding channels ∈ `x|y|series|group_by|value`; `aggregation` ∈ `sum|avg|count|min|max|median|none`; per-type required channels enforced; every referenced `column_id` must belong to the analysis (verified via `analysis_store.list_columns`); unique widget ids; ≤50 widgets. The model never supplies SQL/table/column **names** — only column **ids** we verify. Invalid → `DashboardError` (HTTP 400); unknown analysis/dashboard → `DashboardNotFound` (HTTP 404).
- **Agent tools** (`custom_tools/dashboard_tools.py`): `suggest_dashboard`, `create_dashboard`, `list_dashboards`, `update_dashboard`, `delete_dashboard`.
- **Auto-suggest a starter dashboard (`dashboard_store.suggest_spec`)** — builds a *valid* draft spec from the analysis's columns so the agent doesn't hand-build every widget. Each data column is sniffed numeric vs categorical from its own cell values (tolerating `$`, `,`, `%`); AI columns are never used as KPI/axis candidates. Heuristics: one **KPI** (sum) per numeric column, a **bar** chart of the first numeric (y, sum) grouped by the first categorical (x), and a **table** of the primary columns. Capped by `max_widgets` (default `SUGGEST_MAX_WIDGETS=8`, ≤ `MAX_WIDGETS`). The result is run through `validate_spec`, so it is safe to hand straight to `create_dashboard`/`update_dashboard`. Nothing is persisted unless asked. Exposed as the `suggest_dashboard` tool (with optional `persist`) and `POST /api/analysis/{id}/dashboards/suggest`.
- **REST** (`/api/analysis/{id}/dashboards`, `/api/analysis/{id}/dashboards/suggest`, `/api/analysis/dashboards/{id}`) + the full spec contract live in [frontend-analysis-integration.md](frontend-analysis-integration.md).

## Env knobs

- `ANALYSIS_ROW_CONCURRENCY` (default 4) — rows processed in parallel per run.
- `ANALYSIS_CELL_MAX_CHARS` (default 8000) — cap stored per AI cell value.
- `ANALYSIS_DEFAULT_MODEL` — overrides the default AI-column model.
- `ANALYSIS_CELL_TOOL_RECURSION` (default 12, min 2) — recursion budget for a
  tool-using AI cell (bounds the model↔tool loop per cell).

## Setup / re-run

```bash
python3 scripts/setup_analysis_schema.py     # idempotent: DDL + realtime publication
python3 scripts/setup_dashboards_schema.py    # idempotent: dashboards table + realtime
```

No workflow auto-reload — restart **DeepAgent Server** after changing any of the
backend modules above.
