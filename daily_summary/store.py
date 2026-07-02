"""Create + write the `deal_daily_summaries` table in the prod deal DB
(wfwgatyfzqzrcauatufb). DDL goes through the Supabase Management API (same path as
scripts/setup_*_schema.py, using SUPABASE_ACCESS_TOKEN); row writes go through
PostgREST with the service-role key. Table posture mirrors deal_records: anon +
authenticated get SELECT (browser read + realtime), all writes are service-role.
"""
from __future__ import annotations
import re, json
import requests
from .common import VERIFY, sb_upsert

_MGMT = "https://api.supabase.com/v1/projects/{ref}/database/query"

DDL = """
create extension if not exists pgcrypto;

create table if not exists public.deal_daily_summaries (
    id                   uuid primary key default gen_random_uuid(),
    opp_id               text not null,
    summary_date         date not null,
    window_start         timestamptz,
    window_end           timestamptz,
    account_name         text,
    opp_name             text,
    owner_name           text,
    forecast_category    text,
    stage                text,
    has_activity         boolean not null default false,
    activity_count       integer not null default 0,
    counts               jsonb not null default '{}'::jsonb,
    summary              text,
    summary_source       text,
    activities           jsonb not null default '[]'::jsonb,
    movements            jsonb not null default '[]'::jsonb,
    meetings_avoma       jsonb not null default '[]'::jsonb,
    next_step_text       text,
    next_step_changed_at timestamptz,
    generated_at         timestamptz not null default now(),
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now(),
    unique (opp_id, summary_date)
);
create index if not exists idx_dds_opp  on public.deal_daily_summaries(opp_id);
create index if not exists idx_dds_date on public.deal_daily_summaries(summary_date desc);

-- Realtime: rich payloads on update/delete.
alter table public.deal_daily_summaries replica identity full;

-- Browser read (writes stay service-role only), mirrors deal_records posture.
grant select on public.deal_daily_summaries to anon, authenticated;
"""

REALTIME = """
do $$ begin
  if not exists (select 1 from pg_publication_tables
    where pubname='supabase_realtime' and schemaname='public'
      and tablename='deal_daily_summaries') then
    alter publication supabase_realtime add table public.deal_daily_summaries;
  end if;
end $$;
"""


def _ref(sec: dict) -> str:
    m = re.search(r"https://([a-z0-9]+)\.supabase\.co", sec.get("SUPABASE_URL", ""))
    return m.group(1) if m else sec.get("SUPABASE_PROJECT_REF", "")


def _mgmt(sec: dict, sql: str, label: str):
    ref = _ref(sec)
    token = sec.get("SUPABASE_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("SUPABASE_ACCESS_TOKEN missing from secret")
    r = requests.post(_MGMT.format(ref=ref),
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                      data=json.dumps({"query": sql}), verify=VERIFY, timeout=120)
    if r.status_code >= 300:
        raise RuntimeError(f"[{label}] mgmt {r.status_code}: {r.text[:300]}")
    print(f"[store] {label}: ok")


def ensure_table(sec: dict):
    _mgmt(sec, DDL, "DDL (table+indexes+grants)")
    _mgmt(sec, REALTIME, "realtime publication")


def upsert_summaries(sec: dict, rows: list) -> int:
    if not rows:
        return 0
    # chunk to keep request bodies reasonable
    n = 0
    for i in range(0, len(rows), 100):
        chunk = rows[i:i + 100]
        sb_upsert(sec, "deal_daily_summaries", chunk, on_conflict="opp_id,summary_date")
        n += len(chunk)
    return n
