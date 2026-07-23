-- 0016_mase_chat_documents.sql
-- Agent-authored downloadable documents for Ask Mase: the chat agent's
-- create_document tool writes a .md to S3 (chatdocs/<id>/<file>.md in the knowledge
-- bucket) and records it here; users download via GET /api/deal-engine/documents/{id}
-- (stable same-origin link — deliberately NOT a presigned S3 URL, which would expire
-- with the ECS task role's rotating credentials). Backed by mase_chat_docs.py.
-- RLS: service-role only (enable RLS, add NO policies) — same posture as mase_skills.

create table if not exists public.mase_chat_documents (
    id          uuid primary key default gen_random_uuid(),
    title       text not null,
    filename    text not null,
    s3_key      text not null,
    chat_id     text,
    opp_id      text,
    size_bytes  integer,
    created_at  timestamptz not null default now()
);

create index if not exists mase_chat_documents_chat_idx on public.mase_chat_documents (chat_id);
create index if not exists mase_chat_documents_opp_idx on public.mase_chat_documents (opp_id);

alter table public.mase_chat_documents enable row level security;
