-- 0015_mase_ask_spend_cap.sql
-- Ask-Mase per-user spend cap ($20 per rolling 5h window, admins exempt).
--
-- Behaves like Claude's usage limit: a FIXED window anchored to first use in the
-- period (not a sliding window). The first spend after any gap opens a fresh
-- window at now(); every subsequent charge inside window_start .. window_start+5h
-- accumulates onto the same window; the first charge at/after the window expires
-- rolls a brand-new window. `resets_at` shown to the user = window_start + 5h.
--
-- Identity is the lower-cased user email. The frontend proxy is the identity-aware
-- choke point (injects body.user_email for non-admins, nothing for admins); the
-- backend owns the cost and calls mase_ask_add_spend() to advance the window.
--
-- Posture mirrors mase_skills (0014): RLS ENABLED with NO policies, so ONLY the
-- service role (proxy service-role client + backend) can read/write. The anon /
-- authenticated (VIBE) keys can never see per-user spend.
--
-- Idempotent: safe to re-run. Applied to Supabase the same way as the other
-- migrations here (Management API / setup script); this file is the canonical
-- schema. See the SHARED CONTRACT in the feature spec — names/shapes MUST match.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- 1. Per-user rolling-window ledger.
-- ---------------------------------------------------------------------------
create table if not exists public.mase_ask_window (
    user_email   text primary key,
    window_start timestamptz not null,
    spend_usd    numeric not null default 0,
    updated_at   timestamptz not null default now()
);

-- Service-role only: enable RLS and add NO anon/authenticated policies, mirroring
-- mase_skills. Service role bypasses RLS, so proxy + backend still have full access.
alter table public.mase_ask_window enable row level security;

-- ---------------------------------------------------------------------------
-- 2. Atomic window-advance RPC.
--    p_hours defaults to 5 but the caller passes the config value
--    (ask_mase_window_hours) so the window length stays config-driven.
--
--    Single-statement INSERT ... ON CONFLICT DO UPDATE => the conflicting row is
--    row-locked for the UPDATE, so concurrent charges for the same user cannot
--    lose an update. Returns the EFFECTIVE window_start after the advance.
-- ---------------------------------------------------------------------------
create or replace function public.mase_ask_add_spend(
    p_email text,
    p_cost  numeric,
    p_hours numeric default 5
) returns timestamptz
language plpgsql
as $$
declare
    v_window_start timestamptz;
    v_email        text := lower(p_email);
begin
    insert into public.mase_ask_window (user_email, window_start, spend_usd, updated_at)
    values (v_email, now(), p_cost, now())
    on conflict (user_email) do update
        set
            -- expired (or exactly at boundary) => open a fresh window at now();
            -- otherwise keep the existing anchor.
            window_start = case
                when now() >= public.mase_ask_window.window_start + (p_hours * interval '1 hour')
                    then now()
                else public.mase_ask_window.window_start
            end,
            -- fresh window => reset to this charge (excluded.spend_usd = p_cost);
            -- still inside the window => accumulate onto the running total.
            spend_usd = case
                when now() >= public.mase_ask_window.window_start + (p_hours * interval '1 hour')
                    then excluded.spend_usd
                else public.mase_ask_window.spend_usd + excluded.spend_usd
            end,
            updated_at = now()
    returning window_start into v_window_start;

    return v_window_start;
end;
$$;

-- ---------------------------------------------------------------------------
-- 3. chat_usage attribution: which user a chat's cost belongs to.
--    Additive column + composite index for per-user audit rollups.
-- ---------------------------------------------------------------------------
alter table public.chat_usage
    add column if not exists user_email text;

create index if not exists idx_chat_usage_user_email
    on public.chat_usage (user_email, updated_at);

-- ---------------------------------------------------------------------------
-- 4. Config seeding: the cap ($) and window (hours), tunable live from admin.
--    app_config is a key/value table in the SHARED Supabase (the proxy reads it
--    via lib/config/server.ts, and the backend now reads ask_mase_window_hours
--    from it too). This migration runs against that same DB, so seed the two
--    keys here — guarded so it is a no-op if app_config isn't present, and
--    ON CONFLICT DO NOTHING so a value an admin already set is never overwritten.
--    Both sides still default to 20 / 5 when a key is absent, so the seed only
--    makes the current values explicit and editable.
-- ---------------------------------------------------------------------------
do $$
begin
    if to_regclass('public.app_config') is not null then
        insert into public.app_config (key, value)
        values ('ask_mase_cap_usd', '20'),
               ('ask_mase_window_hours', '5')
        on conflict (key) do nothing;
    end if;
end $$;

-- ===========================================================================
-- REVERSAL (run manually to fully undo this migration):
--
--   drop function if exists public.mase_ask_add_spend(text, numeric, numeric);
--   drop index    if exists public.idx_chat_usage_user_email;
--   alter table   public.chat_usage   drop column if exists user_email;
--   drop table    if exists public.mase_ask_window;
--   delete from public.app_config where key in ('ask_mase_cap_usd','ask_mase_window_hours');
-- ===========================================================================
