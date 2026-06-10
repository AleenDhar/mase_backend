-- Deal Engine: Espresso to-do -> Salesforce push records.
-- One row per ticked-and-confirmed to-do that a rep pushed to Salesforce as a
-- COMPLETED Activity/Task. This table is the idempotency ledger: it makes the
-- push safe to retry (unique todo_key) and lets the /todo view show which items
-- were already pushed (and surface their Salesforce Task Id) across reloads.
--
-- The Salesforce write itself is a DIRECT server-side simple-salesforce call
-- from the push endpoint (NOT the agent / MCP tool catalog), so the agent's
-- Salesforce write lockdown is untouched. This table only records the result.
--
-- Applied in production via scripts/setup_deal_todo_pushes_schema.py (Supabase
-- Management API). This file is the canonical documentation of the schema.
--
-- Posture mirrors deal_records: ALL WRITES go through the backend service-role
-- key; anon/authenticated get SELECT only (browser read + realtime). RLS left
-- disabled to match the project-wide posture (see docs/security-auth.md).

create extension if not exists pgcrypto;

create table if not exists public.deal_todo_pushes (
    id          uuid primary key default gen_random_uuid(),
    todo_key    text not null unique,          -- deterministic fingerprint of the to-do
    opp_id      text not null,                 -- Salesforce Opportunity id (15- or 18-char)
    category    text,                          -- critical | important | explicitRequirements | implicit | bestPractice
    subject     text,                          -- the Salesforce Task Subject we wrote
    sf_task_id  text,                          -- the created Salesforce Task Id
    pushed_by   text,                          -- who confirmed the push (best-effort)
    payload     jsonb not null default '{}'::jsonb,  -- snapshot of the pushed to-do
    pushed_at   timestamptz not null default now(),
    created_at  timestamptz not null default now()
);
create index if not exists idx_deal_todo_pushes_opp
    on public.deal_todo_pushes(opp_id);

-- Realtime: rich payloads on update/delete.
alter table public.deal_todo_pushes replica identity full;

-- Read grants for the browser (writes remain service-role only).
grant select on public.deal_todo_pushes to anon, authenticated;
