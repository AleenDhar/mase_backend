-- 0009_sweep_queue.sql
-- Crash-safe, non-blocking deal-engine sweep.
--
-- The sweep used to run as an asyncio batch INSIDE the web process, with
-- progress held only in an in-memory dict. That starved web requests during a
-- big run and lost all progress on restart. This table makes the queue the
-- DURABLE source of truth: a separate worker.py process drains it, so the web
-- process stays responsive and a crash/restart resumes exactly where it left
-- off (per-opp records are already idempotently upserted by deal_engine_store).
--
-- Applied in production via scripts/setup_sweep_queue_schema.py (Supabase
-- Management API). This file is the canonical documentation of the schema.
--
-- Posture mirrors deal_records / deal_trigger_runs: ALL WRITES go through the
-- backend service-role key; anon/authenticated get SELECT only (browser read +
-- realtime). RLS left disabled to match the project-wide posture.

create extension if not exists pgcrypto;

create table if not exists public.sweep_queue (
    opp_id       text primary key,
    opp_id_15    text,                         -- 15-char SF key (report exports are 15-char)
    run_id       text,                         -- the enqueue batch this row belongs to
    status       text not null default 'waiting',  -- waiting | working | done | failed
    attempts     integer not null default 0,   -- incremented on each claim
    account_name text,
    owner_name   text,
    opp_name     text,
    duration_ms  integer,                       -- last analyze_one duration (for the dashboard)
    error        text,
    claimed_at   timestamptz,                   -- when a worker last claimed it (stale-reclaim cursor)
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create index if not exists idx_sweep_queue_status on public.sweep_queue(status);
create index if not exists idx_sweep_queue_run    on public.sweep_queue(run_id);

-- Atomic claim. PostgREST cannot express FOR UPDATE SKIP LOCKED, so the claim is
-- a single server-side function exposed at POST /rpc/claim_one_sweep. It picks
-- the oldest waiting row, locks it skipping any row another worker already holds,
-- flips it to 'working', bumps attempts and stamps claimed_at — all atomically.
-- Returns the claimed row, or no rows when the queue is drained.
create or replace function public.claim_one_sweep()
returns setof public.sweep_queue
language sql
volatile
as $$
    update public.sweep_queue q
    set status     = 'working',
        attempts   = q.attempts + 1,
        claimed_at = now(),
        updated_at = now()
    where q.opp_id = (
        select s.opp_id
        from public.sweep_queue s
        where s.status = 'waiting'
        order by s.created_at asc, s.updated_at asc
        for update skip locked
        limit 1
    )
    returning q.*;
$$;

-- Idempotent single-opp enqueue (the Salesforce-update trigger path). Inserts a
-- brand-new waiting row, OR re-arms an existing row ONLY if it already finished
-- (done|failed) — a row that is currently waiting/working is left untouched and
-- NOT returned, which the caller reads as "already queued" (natural dedupe on
-- the opp_id primary key).
create or replace function public.enqueue_one_sweep(
    p_opp_id  text,
    p_run_id  text,
    p_account text default null,
    p_owner   text default null,
    p_name    text default null
)
returns setof public.sweep_queue
language sql
volatile
as $$
    insert into public.sweep_queue
        (opp_id, opp_id_15, run_id, status, attempts,
         account_name, owner_name, opp_name, error, claimed_at, created_at, updated_at)
    values
        (p_opp_id, left(p_opp_id, 15), p_run_id, 'waiting', 0,
         p_account, p_owner, p_name, null, null, now(), now())
    on conflict (opp_id) do update
        set status       = 'waiting',
            attempts     = 0,
            run_id       = excluded.run_id,
            account_name = coalesce(excluded.account_name, public.sweep_queue.account_name),
            owner_name   = coalesce(excluded.owner_name,   public.sweep_queue.owner_name),
            opp_name     = coalesce(excluded.opp_name,     public.sweep_queue.opp_name),
            error        = null,
            claimed_at   = null,
            updated_at   = now()
        where public.sweep_queue.status in ('done', 'failed')
    returning *;
$$;

-- The mutating functions are called with the backend service-role key only.
grant execute on function public.claim_one_sweep()      to service_role;
grant execute on function public.enqueue_one_sweep(text, text, text, text, text) to service_role;

-- Realtime: rich payloads on update/delete.
alter table public.sweep_queue replica identity full;

-- Read grants for the browser (writes remain service-role only).
grant select on public.sweep_queue to anon, authenticated;
