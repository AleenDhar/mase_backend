-- Deal Engine: analysis-run audit log.
-- One row per analyze_one() execution (bulk sweep, manual single re-run, or a
-- Salesforce-update trigger). Powers the "what changed & when" dashboard: a
-- list of opps with their latest run, drill-in to the full per-opp run history
-- (time taken, cost, model, token usage, and failures).
--
-- Applied in production via scripts/setup_deal_trigger_runs_schema.py (Supabase
-- Management API). This file is the canonical documentation of the schema.
--
-- Posture mirrors deal_records: ALL WRITES go through the backend service-role
-- key; anon/authenticated get SELECT only (browser read + realtime). RLS left
-- disabled to match the project-wide posture (see docs/security-auth.md).

create extension if not exists pgcrypto;

create table if not exists public.deal_trigger_runs (
    id            uuid primary key default gen_random_uuid(),
    opp_id        text not null,
    opp_id_15     text not null,          -- 15-char SF key (report exports are 15-char)
    opp_name      text,
    account_name  text,
    owner_name    text,
    source        text not null default 'sweep',  -- sweep | manual | salesforce_trigger
    status        text not null,                  -- completed | failed | parse_error
    duration_ms   integer,
    model         text,
    input_tokens  integer,
    output_tokens integer,
    total_tokens  integer,
    cost_usd      numeric,
    error         text,
    created_at    timestamptz not null default now()
);
create index if not exists idx_deal_trigger_runs_opp
    on public.deal_trigger_runs(opp_id_15, created_at desc);
create index if not exists idx_deal_trigger_runs_created
    on public.deal_trigger_runs(created_at desc);

-- Latest run per opp (the dashboard list): one row per opp, newest run.
create or replace view public.deal_trigger_latest as
select distinct on (opp_id_15)
    opp_id_15, opp_id, opp_name, account_name, owner_name,
    source, status, duration_ms, model,
    input_tokens, output_tokens, total_tokens, cost_usd, error,
    created_at as last_run_at
from public.deal_trigger_runs
order by opp_id_15, created_at desc;

-- Realtime: rich payloads on update/delete.
alter table public.deal_trigger_runs replica identity full;

-- Read grants for the browser (writes remain service-role only).
grant select on public.deal_trigger_runs to anon, authenticated;
grant select on public.deal_trigger_latest to anon, authenticated;
