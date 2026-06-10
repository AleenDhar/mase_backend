---
name: Analysis cell fetch/filter/search tools
description: Why purpose-built read tools exist for analysis cells instead of letting the agent use raw supabase_query.
---

# Analysis fetch/filter/search tools

Purpose-built, read-only agent tools cover filtering, string-search, and multi-cell
fetch over analysis cells, so the agent does NOT drop to the generic `supabase_query`
(raw DB) tool for these jobs.

**Why:** `analysis_get` only returns a tiny row preview with no cell values, and
`analysis_query` dumps a position-truncated grid to an LLM. Neither can answer
"which rows have X" / "where is Y mentioned". Lacking a proper path, the agent had
been hand-writing raw queries against the internal `analysis_cells` table via
`supabase_query` — fragile (schema-coupled) and high blast-radius (the generic
Supabase tool can also DML/DDL the same DB that stores the grid).

**How to apply:**
- Filtering rows / opp search / column-value match → `analysis_filter_rows`
  (ops: empty/not_empty/equals/contains/gt/gte/lt/lte; `data_only` omits the
  verbose AI columns; `row_label_contains` = opportunity search). Value/label
  matching runs in Python, so those args are NOT a URL-injection vector.
- Free-text substring across cells → `analysis_search_cells` (ilike pushed to DB;
  the model-supplied term MUST stay URL-encoded in the store layer).
- Full untruncated value of specific cells → `analysis_get_cells` (snippets from
  the other two return `cell_id` for drill-down).
- Store layer keeps table names as module constants (no raw-SQL path). Any
  model-supplied value going into a PostgREST filter string must be URL-encoded,
  and id lists must be UUID-validated + chunked (≤100/req) to avoid URL-length
  failures. Pagination responses carry an explicit `has_more`/`hint` so the agent
  knows to page through.
