-- Deal Engine: token-free hard-fact reconciliation run history.
-- One row per hard_refresh_all() INVOCATION — completed runs, skipped no-ops
-- (another refresh / AI sweep / queue active), and fatal failures alike. The
-- hard refresh previously persisted only the MOST RECENT summary (in-memory +
-- .deal_engine_hard_refresh_last.json); this append-only log keeps a durable
-- trail so the nightly schedule is auditable and an anomalous run (unusually
-- high/low updated/removed counts, or a string of skips/failures) can be
-- spotted over time.
--
-- Mirrors deal_trigger_runs (the per-opp analysis-run log): applied in
-- production via scripts/setup_deal_hard_refresh_runs_schema.py (Supabase
-- Management API). This file is the canonical documentation of the schema.
--
-- Posture mirrors deal_records / deal_trigger_runs: ALL WRITES go through the
-- backend service-role key; anon/authenticated get SELECT only (browser read +
-- realtime). RLS left disabled to match the project-wide posture.

create extension if not exists pgcrypto;

create table if not exists public.deal_hard_refresh_runs (
    id          uuid primary key default gen_random_uuid(),
    source      text not null default 'manual',  -- manual | nightly_cron | <caller>
    status      text not null default 'completed', -- completed | skipped | failed
    records     integer not null default 0,      -- records considered this run
    matched     integer not null default 0,      -- records matched to a live SF opp
    updated     integer not null default 0,      -- records whose hard fields changed
    removed     integer not null default 0,      -- records deleted (back to Initial Interest)
    unmatched   integer not null default 0,      -- records with no live SF match
    failed      integer not null default 0,      -- per-record write failures
    skipped     text,                            -- non-null reason when the run was a no-op skip
    error       text,                            -- fatal failure message (status='failed')
    finished_at timestamptz,                     -- when hard_refresh_all() returned
    created_at  timestamptz not null default now()
);

-- Idempotent column adds for already-created deployments (status/error were
-- introduced after the first cut of this table).
alter table public.deal_hard_refresh_runs
    add column if not exists status text not null default 'completed';
alter table public.deal_hard_refresh_runs
    add column if not exists error text;

create index if not exists idx_deal_hard_refresh_runs_created
    on public.deal_hard_refresh_runs(created_at desc);

-- Realtime: rich payloads on update/delete.
alter table public.deal_hard_refresh_runs replica identity full;

-- Read grants for the browser (writes remain service-role only).
grant select on public.deal_hard_refresh_runs to anon, authenticated;
