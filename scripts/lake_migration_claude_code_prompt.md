# Prompt for Claude Code (frontend / Next.js repo)

Copy everything below the line into Claude Code in the **Next.js frontend repo** (the one that owns Supabase migrations).

---

## Task: Add the `lake.opportunity_diagnoses` Supabase migration

We're standing up a data lake for Opportunity Diagnosis runs. The DeepAgent backend (Replit) already writes to `lake.opportunity_diagnoses` via the Supabase service-role key — but the table doesn't exist yet, so writes silently no-op. We need you to:

1. Create a new Supabase migration that provisions the schema, table, indexes, and PostgREST exposure.
2. Apply it (locally + push to the linked Supabase project).
3. Confirm the schema appears under **Settings → API → Exposed schemas** in the Supabase dashboard.

### Migration SQL (copy verbatim into the new migration file)

```sql
-- lake.opportunity_diagnoses — historical + going-forward diagnosis data lake
-- Idempotent. Safe to re-run.

CREATE SCHEMA IF NOT EXISTS lake;

CREATE TABLE IF NOT EXISTS lake.opportunity_diagnoses (
  id                   bigserial PRIMARY KEY,
  chat_id              text        NOT NULL,
  project_id           text        NOT NULL,
  run_at               timestamptz NOT NULL,
  run_by_user_id       text,
  diagnosis_md         text,

  -- Salesforce-derived (regex extracted by lake.py Path A)
  account_id           text,
  account_name         text,
  opportunity_id       text,
  opportunity_name     text,
  stage                text,
  amount               numeric,
  close_date           date,
  forecast_category    text,
  owner                text,
  owner_name           text,
  products             jsonb,

  -- Avoma-derived
  meeting_count_30d    integer,
  last_meeting_date    timestamptz,
  last_meeting_id      text,

  -- GPT-4o-mini narrative extraction (Path B)
  momentum_verdict     text,
  health_rating        text,
  top_risks            jsonb,
  recommendations      jsonb,

  created_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT opportunity_diagnoses_chat_run_uniq UNIQUE (chat_id, run_at)
);

CREATE INDEX IF NOT EXISTS opportunity_diagnoses_account_id_run_at_idx
  ON lake.opportunity_diagnoses (account_id, run_at DESC);

CREATE INDEX IF NOT EXISTS opportunity_diagnoses_project_id_idx
  ON lake.opportunity_diagnoses (project_id);

-- Grants for the standard Supabase roles so the service-role JWT used by the
-- Python supabase client can read/write through PostgREST.
GRANT USAGE ON SCHEMA lake TO postgres, anon, authenticated, service_role;
GRANT ALL ON ALL TABLES    IN SCHEMA lake TO postgres, anon, authenticated, service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA lake TO postgres, anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA lake
  GRANT ALL ON TABLES    TO postgres, anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA lake
  GRANT ALL ON SEQUENCES TO postgres, anon, authenticated, service_role;

-- Expose `lake` to PostgREST so .schema("lake") calls succeed
-- (otherwise the supabase client returns PGRST106 "Invalid schema").
ALTER ROLE authenticator SET pgrst.db_schemas = 'public, graphql_public, lake';
NOTIFY pgrst, 'reload config';
```

### Steps

1. Create the migration file in the conventional location for this repo, e.g.:
   ```
   supabase/migrations/<timestamp>_lake_opportunity_diagnoses.sql
   ```
   Use the next timestamp prefix that fits the existing convention.

2. Apply locally:
   ```
   supabase db push        # or: supabase migration up
   ```
   and confirm the migration runs cleanly with no errors.

3. Push to the linked hosted project:
   ```
   supabase db push --linked
   ```
   (or whatever the team's promotion command is — match existing migration workflow).

4. **Manual dashboard step (important):** open the Supabase project dashboard → **Settings → API → Exposed schemas**, and make sure `lake` is in the list. The `ALTER ROLE` above tries to set this, but the dashboard setting is authoritative on hosted Supabase.

### Verification

After the migration is live, run this query in the Supabase SQL editor (should return zero rows but no error):

```sql
SELECT count(*) FROM lake.opportunity_diagnoses;
```

And confirm via the REST API (substitute your project ref + service-role key):

```bash
curl -s "https://<ref>.supabase.co/rest/v1/opportunity_diagnoses?select=count" \
  -H "apikey: <SERVICE_KEY>" \
  -H "Authorization: Bearer <SERVICE_KEY>" \
  -H "Accept-Profile: lake"
```

A `200` response (with `[{"count":0}]`) means PostgREST sees the schema. A `406` / `PGRST106` means the schema isn't exposed yet.

### Hand-back

Once the migration is applied **and** `lake` is in the exposed-schemas list, ping the backend (Replit) team. They will then run `python3 scripts/backfill_lake.py` to populate ~83 historical rows and verify per-project counts.

### Out of scope

- Don't add RLS policies — the lake is service-role-only for now (no end-user reads).
- Don't add foreign keys to `chats` — `chat_id` is intentionally a loose reference; some old chats in the lake may not exist in `public.chats` anymore.
- Don't seed any rows. Backfill happens on the Replit side.
