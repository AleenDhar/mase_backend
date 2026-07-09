-- sf-report-watch: state for the scheduled Salesforce-report → VIBE-project poller.
--
-- Backs infra/sf-report-watch/lambda_function.py. That Lambda runs on an
-- EventBridge schedule (every 5 min), queries the Salesforce object behind the
-- "APAC GTM MQL Global_V1" report (MQL_History__c, APAC-account MQLs), and for
-- each NEW row POSTs to VIBE /api/workflows/dispatch-abm to kick a project run.
--
-- Two tables:
--   sf_report_watch_cursor — one row per watched report; the high-water mark
--     (max Salesforce CreatedDate already processed). Seeded to "now" on first
--     run so an initial deploy does NOT dispatch the existing backlog.
--   sf_report_watch_log — one row per MQL_History__c record we've seen; the
--     dedup ledger (PK = the SF record id) + an audit trail of what was
--     dispatched, to which BDR, and the resulting VIBE chat_id.
--
-- Posture mirrors deal_trigger_runs: ALL WRITES go through the poller's
-- service-role key; anon/authenticated get SELECT only. RLS left disabled to
-- match the project-wide posture (see docs/security-auth.md). Canonical schema
-- doc — apply via the Supabase Management API / execute_sql like the others.

create extension if not exists pgcrypto;

-- High-water mark per watched report (keyed by the SF report id / label).
create table if not exists public.sf_report_watch_cursor (
    report_id   text primary key,          -- e.g. '00OP7000005v4TsMAI'
    watermark   timestamptz not null,       -- max SF CreatedDate already processed
    updated_at  timestamptz not null default now()
);

-- Dedup ledger + dispatch audit. One row per MQL_History__c record ever seen.
create table if not exists public.sf_report_watch_log (
    mqlh_id        text primary key,        -- MQL_History__c Id (18-char) — the dedup key
    report_id      text not null,
    contact_id     text,
    contact_name   text,
    account_id     text,
    account_name   text,
    bdr_email      text,                    -- resolved owner email used for dispatch
    bdr_name       text,
    campaign_type  text,
    mql_status     text,
    mql_score      numeric,
    mql_date_time  timestamptz,             -- MQL_History__c.MQL_Date_Time__c
    created_date   timestamptz,             -- SF CreatedDate (drives the watermark)
    chat_id        text,                    -- chat created by dispatch-abm, if any
    status         text not null,           -- dispatched | dry_run | skipped_no_bdr | failed
    error          text,
    dispatched_at  timestamptz not null default now()
);
create index if not exists idx_sf_report_watch_log_report
    on public.sf_report_watch_log(report_id, created_date desc);
create index if not exists idx_sf_report_watch_log_status
    on public.sf_report_watch_log(status, dispatched_at desc);

-- Realtime: rich payloads on update/delete (optional dashboard).
alter table public.sf_report_watch_cursor replica identity full;
alter table public.sf_report_watch_log    replica identity full;

-- Read grants for the browser (writes remain service-role only).
grant select on public.sf_report_watch_cursor to anon, authenticated;
grant select on public.sf_report_watch_log    to anon, authenticated;
