-- MASE Teams bot control room.
-- Three tables, all written ONLY by the backend via the service-role key (same posture
-- as the other MASE tables — RLS left disabled; no anon/authenticated grants needed).
--
--   teams_bot_allowlist  — who may use the bot (gate on /api/messages).
--   teams_bot_activity   — recent bot activity, shown in the control-room log.
--   teams_bot_settings   — key/value flags (enforce_allowlist, history_enabled).

create table if not exists public.teams_bot_allowlist (
    id            uuid primary key default gen_random_uuid(),
    email         text,                    -- stored lower-cased
    aad_object_id text,                    -- Entra object id, when known
    display_name  text,
    enabled       boolean not null default true,
    added_by      text,
    added_at      timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
create unique index if not exists uq_teams_allowlist_email
    on public.teams_bot_allowlist(email) where email is not null;
create index if not exists idx_teams_allowlist_aad
    on public.teams_bot_allowlist(aad_object_id);

create table if not exists public.teams_bot_activity (
    id                uuid primary key default gen_random_uuid(),
    ts                timestamptz not null default now(),
    conversation_id   text,
    conversation_type text,               -- personal | groupChat | channel
    user_name         text,
    user_email        text,
    direction         text,               -- in | out
    status            text,               -- ok | denied | ignored | error
    text              text,
    detail            text
);
create index if not exists idx_teams_activity_ts on public.teams_bot_activity(ts desc);

create table if not exists public.teams_bot_settings (
    key        text primary key,
    value      text,
    updated_at timestamptz not null default now()
);

-- Defaults: enforcement OFF (so deploying this never locks anyone out — an admin turns it
-- ON from the control room after adding users); history OFF (blocked on the metered Graph API).
insert into public.teams_bot_settings(key, value) values
    ('enforce_allowlist', 'false'),
    ('history_enabled',   'false')
on conflict (key) do nothing;
