-- ============================================================================
-- DATALAKE: Avoma calls + transcripts, searchable.
-- Apply to the NEW Supabase project ("datalake"). Separate from the MASE app DB.
-- Goal: every org call for the last ~2 years stored with full transcript + AI
-- notes, searchable by content (full-text + fuzzy) and linkable to a deal by
-- CRM id / buyer domain / attendee email.
-- ============================================================================
create extension if not exists pg_trgm;

-- One row per Avoma meeting (the call header / metadata + CRM association).
create table if not exists avoma_meetings (
  uuid               text primary key,            -- Avoma meeting uuid
  subject            text,
  start_at           timestamptz,
  end_at             timestamptz,
  duration           numeric,                      -- Avoma sends this as a float
  state              text,                         -- completed / not_recorded / bot_denied_entry / ...
  recording_state    text,
  transcript_ready   boolean,
  notes_ready        boolean,
  is_call            boolean,
  is_internal        boolean,
  organizer_email    text,
  attendees          jsonb,                        -- full attendee objects
  attendee_emails    text[],                       -- lower-cased emails (for matching)
  attendee_domains   text[],                       -- lower-cased domains (buyer match)
  crm_opportunity_id text,                         -- 18-char SF Opp Id (from crm_associations)
  crm_account_id     text,                         -- 18-char SF Account Id
  crm_contact_ids    jsonb,
  purpose            text,
  outcome            text,
  url                text,
  created            timestamptz,
  modified           timestamptz,                  -- Avoma last-modified (sync watermark source)
  synced_at          timestamptz default now(),
  raw                jsonb                         -- full Avoma meeting payload (forensics)
);
create index if not exists idx_meet_account on avoma_meetings (crm_account_id);
create index if not exists idx_meet_opp     on avoma_meetings (crm_opportunity_id);
create index if not exists idx_meet_start   on avoma_meetings (start_at desc);
create index if not exists idx_meet_modified on avoma_meetings (modified desc);
create index if not exists idx_meet_domains on avoma_meetings using gin (attendee_domains);
create index if not exists idx_meet_emails  on avoma_meetings using gin (attendee_emails);

-- One row per transcript. transcript_text is the flattened, speaker-attributed
-- text used for search. `ts` is a generated full-text vector over it.
create table if not exists avoma_transcripts (
  meeting_uuid       text primary key references avoma_meetings (uuid) on delete cascade,
  transcription_uuid text,
  transcript         jsonb,                        -- raw structured transcript (segments)
  transcript_text    text,                         -- flattened "Speaker: text" for search
  speakers           jsonb,
  vtt_url            text,
  synced_at          timestamptz default now(),
  ts                 tsvector generated always as (to_tsvector('english', coalesce(transcript_text, ''))) stored
);
create index if not exists idx_transcript_fts  on avoma_transcripts using gin (ts);
create index if not exists idx_transcript_trgm on avoma_transcripts using gin (transcript_text gin_trgm_ops);

-- One row per meeting's AI notes / insights (the Avoma summary).
create table if not exists avoma_insights (
  meeting_uuid   text primary key references avoma_meetings (uuid) on delete cascade,
  ai_notes       jsonb,
  ai_notes_text  text,
  keywords       jsonb,
  synced_at      timestamptz default now(),
  ts             tsvector generated always as (to_tsvector('english', coalesce(ai_notes_text, ''))) stored
);
create index if not exists idx_insights_fts on avoma_insights using gin (ts);

-- Backfill / incremental-sync bookkeeping so the 2-year pull is resumable.
create table if not exists avoma_sync_state (
  id                 text primary key,             -- e.g. 'backfill' / 'incremental'
  watermark          timestamptz,                  -- highest modified synced so far
  last_page          int,
  meetings_seen      int  default 0,
  transcripts_synced int  default 0,
  status             text default 'idle',
  updated_at         timestamptz default now()
);

-- Convenience: search transcripts and get the call header in one go.
create or replace view avoma_call_search as
  select m.uuid, m.subject, m.start_at, m.crm_account_id, m.crm_opportunity_id,
         m.attendee_emails, t.transcript_text, t.ts
  from avoma_meetings m
  join avoma_transcripts t on t.meeting_uuid = m.uuid;
