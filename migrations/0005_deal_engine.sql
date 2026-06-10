-- Deal Intelligence Engine schema (Deals / Espresso / Matcha / Chat).
-- One evidence-anchored record per opportunity (the "book"). Hard columns come
-- straight from Salesforce; the AI columns are produced by the sweep agent and
-- stored as a single canonical JSON document in `record`.
--
-- Applied in production via scripts/setup_deal_engine_schema.py (Supabase
-- Management API). This file is the canonical documentation of the schema.
--
-- Posture: ALL WRITES go through the backend service-role key. anon /
-- authenticated get SELECT only (so the browser can read + subscribe to
-- realtime). RLS is left DISABLED to match the current project-wide posture
-- (see docs/security-auth.md); add scoped policies before production hardening.

create extension if not exists pgcrypto;

-- deal_records ---------------------------------------------------------------
-- `record` holds the full canonical document:
--   { opp_id, swept_at, schema_version, analysis_confidence, forecast_critical,
--     hard: {...salesforce facts...},
--     ai:   { north_star_verdict, deal_movement, competitive_position,
--             customer_expectations_fit, explicit_requirements,
--             implicit_requirements, gaps, best_practice_check, stakeholder_map,
--             champion_strength, ai_positioning_strength, ai_fit_signal,
--             vulnerabilities, open_deliverables, confidence_signals,
--             recommended_moves, evidence_coverage } }
-- The flat columns below mirror a few `hard`/top-level fields so the table can
-- be filtered/sorted cheaply (Deals table, owner filter, Matcha rollups).
create table if not exists public.deal_records (
    opp_id              text primary key,
    owner_name          text,
    account_name        text,
    opp_name            text,
    stage               text,
    forecast_category   text,
    amount              numeric,
    close_date          date,
    qualified_date      date,
    last_activity_date  date,
    forecast_critical   boolean not null default false,
    analysis_confidence text,
    swept_at            date,
    record              jsonb not null default '{}'::jsonb,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);
create index if not exists idx_deal_records_owner on public.deal_records(owner_name);
create index if not exists idx_deal_records_stage on public.deal_records(stage);

-- Realtime: rich payloads on update/delete.
alter table public.deal_records replica identity full;

-- Read grants for the browser (writes remain service-role only).
grant select on public.deal_records to anon, authenticated;
