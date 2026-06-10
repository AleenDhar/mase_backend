-- Large MCP tool-response archive.
-- One row per oversized tool response (those exceeding
-- TOOL_RESPONSE_SUMMARIZE_THRESHOLD). Previously these were written to
-- mcp_output/*.json on the container filesystem; on AWS ECS Fargate the
-- filesystem is ephemeral, so the full payloads now live here instead. The
-- agent still only sees the summarised/truncated version — this table is the
-- durable archive of the complete response for audit/replay/debugging.
--
-- Written best-effort and fire-and-forget by archive_tool_output() in
-- server.py (service-role key). Also receives raw SNS webhook envelopes
-- (tool_name = 'sns_<type>').
--
-- Posture mirrors the other tables: ALL WRITES go through the backend
-- service-role key; anon/authenticated get SELECT only. RLS left disabled to
-- match the project-wide posture (see docs/security-auth.md).

create extension if not exists pgcrypto;

create table if not exists public.mcp_tool_outputs (
    id           uuid primary key default gen_random_uuid(),
    chat_id      text,                 -- originating chat UUID when available
    tool_name    text not null,        -- MCP tool name, or 'sns_<type>'
    size_chars   integer,              -- length of the serialised response
    payload      jsonb,                -- full response when it is a dict/list
    payload_text text,                 -- full response when it is a scalar/string
    created_at   timestamptz not null default now()
);
create index if not exists idx_mcp_tool_outputs_chat
    on public.mcp_tool_outputs(chat_id, created_at desc);
create index if not exists idx_mcp_tool_outputs_tool
    on public.mcp_tool_outputs(tool_name, created_at desc);
create index if not exists idx_mcp_tool_outputs_created
    on public.mcp_tool_outputs(created_at desc);

-- Read grants for the browser (writes remain service-role only).
grant select on public.mcp_tool_outputs to anon, authenticated;
