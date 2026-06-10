-- Task #11 — lake.opportunity_diagnoses migration
-- Run this in the Supabase Dashboard SQL editor (project that SUPABASE_URL points at).
-- Safe to re-run (all statements are idempotent).
--
-- NOTE: This file is the canonical source of the schema and is kept in sync
-- with what's deployed in the frontend's Supabase project (commits c603df7
-- + a52a36e on the Next.js side).  Column names below match exactly what
-- lake.py's _extract_*_fields functions produce.

CREATE SCHEMA IF NOT EXISTS lake;

CREATE TABLE IF NOT EXISTS lake.opportunity_diagnoses (
  id                   bigserial PRIMARY KEY,
  chat_id              text        NOT NULL,
  project_id           text        NOT NULL,
  run_at               timestamptz NOT NULL,
  run_by_user_id       text,
  diagnosis_md         text,

  -- Salesforce-derived (Path A regex extraction in lake.py)
  account_id           text,
  account_name         text,
  opportunity_id       text,
  opportunity_name     text,
  stage                text,
  amount               numeric,
  close_date           date,
  forecast_category    text,        -- reserved; not yet populated
  owner                text,        -- raw OwnerId
  owner_name           text,        -- resolved from nested Owner.Name
  products             jsonb,

  -- Avoma-derived
  meeting_count_30d    integer,
  last_meeting_date    timestamptz,
  last_meeting_id      text,        -- reserved; not yet populated by extractor

  -- GPT-4o-mini narrative extraction (Path B)
  momentum_verdict     text,        -- "accelerating" | "stalling" | "drifting"
  health_rating        text,        -- "high" | "medium" | "low"
  top_risks            jsonb,       -- array of {title, dynamic, signal}
  recommendations      jsonb,       -- array of {what, why, who, when}
  key_themes           jsonb,       -- reserved; not yet populated by extractor

  created_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT opportunity_diagnoses_chat_run_uniq UNIQUE (chat_id, run_at)
);

CREATE INDEX IF NOT EXISTS opportunity_diagnoses_account_id_run_at_idx
  ON lake.opportunity_diagnoses (account_id, run_at DESC);

CREATE INDEX IF NOT EXISTS opportunity_diagnoses_project_id_idx
  ON lake.opportunity_diagnoses (project_id);

-- Grant access to the standard Supabase roles so the service-role JWT
-- (used by the Python supabase client) can read/write through PostgREST.
GRANT USAGE ON SCHEMA lake TO postgres, anon, authenticated, service_role;
GRANT ALL ON ALL TABLES    IN SCHEMA lake TO postgres, anon, authenticated, service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA lake TO postgres, anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA lake
  GRANT ALL ON TABLES    TO postgres, anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA lake
  GRANT ALL ON SEQUENCES TO postgres, anon, authenticated, service_role;

-- Expose the `lake` schema to PostgREST so the supabase client's
-- `.schema("lake")` calls succeed (otherwise PGRST106 "Invalid schema").
-- After this runs, ALSO go to Supabase Dashboard → Settings → API →
-- "Exposed schemas" and add `lake` (the dashboard setting overrides this
-- in some hosted setups).
ALTER ROLE authenticator SET pgrst.db_schemas = 'public, graphql_public, lake';
NOTIFY pgrst, 'reload config';
