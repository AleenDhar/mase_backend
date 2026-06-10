-- 0008_deal_records_membership.sql
-- Make the Salesforce report "MASE Opportunity V1" the single source of truth
-- for Deal Engine book membership. A deal record is now either ACTIVE (in the
-- report) or inactive (left the report) — we soft-deactivate instead of deleting
-- so the record + its history survive and a re-entrant can simply reactivate.
--
-- Idempotent / additive: safe to re-run. Existing rows default to active=true so
-- the current book is preserved until the first reconcile runs.

alter table public.deal_records
  add column if not exists active boolean not null default true;

alter table public.deal_records
  add column if not exists removed_at timestamptz;

-- The hot path (every list view) filters active=true; index it.
create index if not exists idx_deal_records_active
  on public.deal_records (active);
