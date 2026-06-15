-- Deal Engine: anti-fabrication validation counter on the run audit log.
--
-- Task #89 makes the sweep structurally incapable of fabricating facts: a pure-code
-- gate inside analyze_one() overrides the deal owner's manager from live Salesforce,
-- drops structured people that are neither a known Salesforce/Avoma contact nor carry
-- a source, and scrubs template/placeholder leakage before the record is persisted.
-- This column records, per run, how many such fabrications the gate caught and
-- neutralized, so the update-log dashboard can surface "records sanitized".
--
-- Applied in production via scripts/setup_deal_trigger_validation_schema.py (Supabase
-- Management API). Additive + idempotent. Safe to re-run.

alter table public.deal_trigger_runs
    add column if not exists validation_violations integer not null default 0;

-- Surface the counter on the latest-run-per-opp view too (used by the dashboard list).
-- NOTE: `create or replace view` only allows APPENDING columns at the end of the
-- existing column list, so validation_violations goes LAST (after last_run_at).
-- The dashboard reads by column name, so position is irrelevant to consumers.
create or replace view public.deal_trigger_latest as
select distinct on (opp_id_15)
    opp_id_15, opp_id, opp_name, account_name, owner_name,
    source, status, duration_ms, model,
    input_tokens, output_tokens, total_tokens, cost_usd, error,
    created_at as last_run_at,
    validation_violations
from public.deal_trigger_runs
order by opp_id_15, created_at desc;

grant select on public.deal_trigger_latest to anon, authenticated;
