-- 0014_mase_skills.sql
-- MASE Skills: admin-authored, load-on-demand PROCEDURES for the RevOps chat agent.
-- A skill = {name, "when to use" description, Markdown body}. The chat agent always
-- sees the (name, description) index and pulls the full body via the load_skill(name)
-- tool only when a request matches (Anthropic "Skills" progressive-disclosure model).
--
-- Distinct from the knowledge base (mase_documents/mase_document_chunks): that store is
-- reference DATA retrieved by vector similarity; a skill is an INSTRUCTION the agent
-- follows. RLS-locked to the service role, mirroring the mase_documents isolation
-- (VIBE / the anon key can't see it). Backed by mase_skills.py + /api/deal-engine/skills/*.

create table if not exists public.mase_skills (
    id              uuid primary key default gen_random_uuid(),
    name            text not null unique,
    description     text not null default '',
    body            text not null,
    enabled         boolean not null default true,
    source_filename text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create index if not exists mase_skills_enabled_idx on public.mase_skills (enabled);

-- Service-role only: enable RLS and add NO anon/authenticated policies, so only the
-- backend's service key (analysis_store) can read/write — same posture as mase_documents.
alter table public.mase_skills enable row level security;
