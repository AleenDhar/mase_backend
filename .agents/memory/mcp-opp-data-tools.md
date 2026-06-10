---
name: MCP opportunity + Avoma data tools
description: Read-only opp/meeting tools on POST /mcp, their data-linkage gotchas, and the unauth testing toggle.
---

# Opportunity + Avoma meeting data on POST /mcp

First-class read-only `@_mcp.tool()` wrappers expose opportunity_cache,
field_history_cache, meeting_cache, avoma_event_reports, and
lake.opportunity_diagnoses by exact name in tools/list. They delegate (via
run_in_executor) to module-level `read_*` functions in `cache_qa.py` and
`lake.py` — those functions are the single source of truth shared with the
"Ask anything" UI closures. Keep that delegation; don't re-inline SQL.

## Data-linkage gotcha (a 0-count is often REAL, not a bug)
- `avoma_event_reports.sf_opportunity_id` is NULL on a meeting only when Avoma's
  meeting object had no `"oppo"` crm_association at webhook time (no_sf_links /
  completed_no_opportunity) OR the Avoma fetch failed (status=failed). The stored
  `raw_sns_envelope` does NOT carry crm_associations, so Avoma's
  `/meetings/{uuid}/?include_crm_associations=true` is the ONLY source of the opp
  link — and many old meetings now 404 (aged out), so they are permanently
  unrecoverable. Re-resolution yield is low (~few opps per few hundred rows).
- `meeting_cache.opportunity_ids` (array, queried with `.contains`) was
  populated ONLY from the SF pull's opp row, so it dropped the link whenever the
  pull returned nothing even though sf_opportunity_id was resolved. FIXED:
  `cache_sync.update_cache_from_report` now falls back to
  `report["sf_opportunity_id"]` (server.py `_run_sf_pull_and_cache` passes it).
- `opportunity_cache` columns are `opportunity_name`/`stage_name` (NOT
  `name`/`stage`) — easy to misread as empty when you query the wrong key.
**How to apply:** before "fixing" an empty result, query the raw table to
confirm the linkage column is actually populated for that id. To repair links in
bulk re-run `scripts/backfill_meeting_opp_links.py` (idempotent: Pass A Avoma
re-resolve, Pass B meeting_cache repair, Pass C opp meetings_count recompute).

## MCP_ALLOW_UNAUTH toggle
`_MCPGateway` skips the Bearer gate entirely when env `MCP_ALLOW_UNAUTH` is
truthy (`1/true/yes/on`). Set in the **development** environment only for
testing; production stays Bearer-gated. Delete the dev env var to re-require
auth in dev.
**Why:** user asked for unauth access to test the /mcp opp/Avoma tools without
a token, without weakening prod auth.
