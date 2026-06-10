-- One-time migration for the agent audit log.
--
-- IMPORTANT: supabase_ddl rejects multi-statement SQL. Apply each
-- statement below as a SEPARATE supabase_ddl call.
-- Order matters: the table must exist before its indexes.
--
-- Every successful or failed call to supabase_execute / supabase_ddl
-- appends a row to public.agent_audit so agent-driven changes to the
-- database are traceable after the fact.

-- Statement 1 of 4: create the audit table.
CREATE TABLE IF NOT EXISTS public.agent_audit (
    id            BIGSERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tool          TEXT NOT NULL,
    sql_text      TEXT NOT NULL,
    affected_rows INTEGER,
    chat_id       TEXT,
    success       BOOLEAN NOT NULL,
    error         TEXT
);

-- Statement 2 of 4: index for "show me the most recent activity".
CREATE INDEX IF NOT EXISTS agent_audit_ts_idx ON public.agent_audit (ts DESC);

-- Statement 3 of 4: index for "what did this chat do".
CREATE INDEX IF NOT EXISTS agent_audit_chat_id_idx ON public.agent_audit (chat_id);

-- Statement 4 of 4: index for "split execute vs ddl".
CREATE INDEX IF NOT EXISTS agent_audit_tool_idx ON public.agent_audit (tool);
