# Supabase MCP server
`supabase_mcp_server.py` exposes 7 tools via Supabase Management API (`/v1/projects/{ref}/database/query`), gated by `DISPATCH_SECRET` Bearer at `/mcp`:
- Read-only: `supabase_list_schemas`, `supabase_list_tables`, `supabase_describe_table`, `supabase_query`, `supabase_table_row_count`.
- DML: `supabase_execute` (INSERT/UPDATE/DELETE/UPSERT/MERGE).
- DDL: `supabase_ddl` (CREATE/ALTER/DROP/TRUNCATE/GRANT/REVOKE/RENAME/COMMENT/REINDEX/VACUUM/ANALYZE/CLUSTER/REFRESH).
SQL classified by leading keyword (comments stripped); multi-statement rejected. Every write appended to `public.agent_audit`. Requires `SUPABASE_PROJECT_REF` + `SUPABASE_ACCESS_TOKEN`.
