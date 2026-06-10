-- verifier_reports — structured verdicts produced by the verifier agent.
--
-- The verifier ALSO writes a chat_messages row (type='verifier_report') for
-- the inline UI; this table is the queryable, project-scoped audit log.
--
-- Safe to apply or skip — `verifier.runner.persist_verdict` silently
-- degrades to chat_messages-only when this table isn't present.

CREATE TABLE IF NOT EXISTS public.verifier_reports (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id         uuid NOT NULL,
    project_id      uuid,
    flow            text NOT NULL,
    flow_version    text NOT NULL,
    passed          boolean NOT NULL,
    missed_ids      jsonb NOT NULL DEFAULT '[]'::jsonb,
    total_tool_calls integer NOT NULL DEFAULT 0,
    summary         text,
    report          jsonb NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS verifier_reports_chat_id_idx
    ON public.verifier_reports (chat_id, created_at DESC);

CREATE INDEX IF NOT EXISTS verifier_reports_project_id_idx
    ON public.verifier_reports (project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS verifier_reports_passed_idx
    ON public.verifier_reports (flow, passed, created_at DESC);
