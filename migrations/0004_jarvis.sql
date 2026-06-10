-- Jarvis global settings — the cross-analysis "enabled analyses" toggle list.
-- Jarvis is a single agent that reads across MANY analyses at once, restricted to
-- the analysis ids stored here. There is exactly ONE global singleton row
-- (id = 'global'); the frontend settings tab toggles which analyses are in it.
-- Applied in production via scripts/setup_jarvis_schema.py (Supabase Management
-- API, same approach as scripts/setup_dashboards_schema.py). Safe to re-run.
--
-- Posture matches the rest of the project: ALL WRITES go through the backend
-- service-role key; anon / authenticated get SELECT only (so the browser can read
-- + subscribe to realtime). RLS is left DISABLED to match the project-wide
-- posture (see docs/security-auth.md).

create table if not exists public.jarvis_settings (
    id                    text primary key default 'global',
    enabled_analysis_ids  jsonb not null default '[]'::jsonb,
    system_prompt         text not null default '',
    updated_at            timestamptz not null default now()
);

-- Editable Jarvis system prompt (the persona/instructions edited from the
-- settings tab). Added separately so re-running against an existing table that
-- predates this column upgrades it in place. Empty string => backend default.
alter table public.jarvis_settings
  add column if not exists system_prompt text not null default '';

-- Seed the singleton row so reads always find it.
insert into public.jarvis_settings (id) values ('global')
  on conflict (id) do nothing;

-- Realtime: rich payloads on update.
alter table public.jarvis_settings replica identity full;

-- Read grant for the browser (writes remain service-role only).
grant select on public.jarvis_settings to anon, authenticated;
