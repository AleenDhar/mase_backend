-- Analysis feature schema (Task #52).
-- Spreadsheet-style analyses over many Salesforce opportunities.
-- Applied in production via scripts/setup_analysis_schema.py (Supabase
-- Management API). This file is the canonical documentation of the schema.
--
-- Posture: ALL WRITES go through the backend service-role key. anon /
-- authenticated get SELECT only (so the browser can read + subscribe to
-- realtime). RLS is left DISABLED to match the current project-wide posture
-- (see docs/security-auth.md); add scoped policies before production hardening.

create extension if not exists pgcrypto;

-- 1. analyses -----------------------------------------------------------------
create table if not exists public.analyses (
    id            uuid primary key default gen_random_uuid(),
    project_id    text,
    chat_id       text,
    title         text not null default 'Untitled analysis',
    description   text,
    status        text not null default 'draft',   -- draft | running | done | error
    source_config jsonb not null default '{}'::jsonb,
    created_by    text,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
create index if not exists idx_analyses_project on public.analyses(project_id);
create index if not exists idx_analyses_chat    on public.analyses(chat_id);

-- 2. analysis_columns ---------------------------------------------------------
create table if not exists public.analysis_columns (
    id          uuid primary key default gen_random_uuid(),
    analysis_id uuid not null references public.analyses(id) on delete cascade,
    name        text not null,
    position    int  not null default 0,
    type        text not null default 'data',      -- data | ai
    config      jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index if not exists idx_analysis_columns_analysis on public.analysis_columns(analysis_id, position);

-- 3. analysis_rows ------------------------------------------------------------
create table if not exists public.analysis_rows (
    id          uuid primary key default gen_random_uuid(),
    analysis_id uuid not null references public.analyses(id) on delete cascade,
    position    int  not null default 0,
    entity_ref  text,                              -- e.g. Salesforce opportunity_id
    label       text,
    source      jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now()
);
create index if not exists idx_analysis_rows_analysis on public.analysis_rows(analysis_id, position);

-- 4. analysis_cells -----------------------------------------------------------
create table if not exists public.analysis_cells (
    id          uuid primary key default gen_random_uuid(),
    analysis_id uuid not null references public.analyses(id) on delete cascade,
    row_id      uuid not null references public.analysis_rows(id) on delete cascade,
    column_id   uuid not null references public.analysis_columns(id) on delete cascade,
    value       text,
    status      text not null default 'empty',     -- empty | pending | running | done | error
    error       text,
    model_used  text,
    tokens_used int,
    updated_at  timestamptz not null default now(),
    unique (row_id, column_id)
);
create index if not exists idx_analysis_cells_analysis on public.analysis_cells(analysis_id);
create index if not exists idx_analysis_cells_row      on public.analysis_cells(row_id);

-- 5. analysis_runs ------------------------------------------------------------
create table if not exists public.analysis_runs (
    id          uuid primary key default gen_random_uuid(),
    analysis_id uuid not null references public.analyses(id) on delete cascade,
    status      text not null default 'running',   -- running | done | error | stopped
    cells_total int not null default 0,
    cells_done  int not null default 0,
    cells_error int not null default 0,
    error       text,
    started_at  timestamptz not null default now(),
    finished_at timestamptz
);
create index if not exists idx_analysis_runs_analysis on public.analysis_runs(analysis_id, started_at desc);

-- Realtime: rich payloads on update/delete for cells + rows + runs.
alter table public.analysis_cells replica identity full;
alter table public.analysis_rows  replica identity full;
alter table public.analysis_runs  replica identity full;

-- Read grants for the browser (writes remain service-role only).
grant select on public.analyses, public.analysis_columns, public.analysis_rows,
                 public.analysis_cells, public.analysis_runs
    to anon, authenticated;
