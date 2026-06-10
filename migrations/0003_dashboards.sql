-- Analysis dashboards schema (Task #53).
-- A dashboard turns a completed analysis into a set of chart/widget specs that
-- the separate Vercel frontend renders. One analysis can have many dashboards.
-- Applied in production via scripts/setup_dashboards_schema.py (Supabase
-- Management API). This file is the canonical documentation of the schema.
--
-- Posture: ALL WRITES go through the backend service-role key. anon /
-- authenticated get SELECT only (so the browser can read + subscribe to
-- realtime). RLS is left DISABLED to match the current project-wide posture
-- (see docs/security-auth.md); add scoped policies before production hardening.
--
-- The `spec` jsonb is validated against an allowlist of widget + aggregation
-- types in dashboard_store.validate_spec BEFORE it is ever written here — the
-- model never supplies raw SQL or table names; widgets bind to analysis columns
-- by column_id only.

create extension if not exists pgcrypto;

-- dashboards ------------------------------------------------------------------
create table if not exists public.dashboards (
    id           uuid primary key default gen_random_uuid(),
    analysis_id  uuid not null references public.analyses(id) on delete cascade,
    project_id   text,
    title        text not null default 'Untitled dashboard',
    description  text,
    spec         jsonb not null default '{"version": 1, "widgets": []}'::jsonb,
    created_by   text,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);
create index if not exists idx_dashboards_analysis on public.dashboards(analysis_id);
create index if not exists idx_dashboards_project  on public.dashboards(project_id);

-- Realtime: rich payloads on update/delete.
alter table public.dashboards replica identity full;

-- Read grants for the browser (writes remain service-role only).
grant select on public.dashboards to anon, authenticated;
