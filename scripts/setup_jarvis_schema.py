"""One-off / idempotent: create the jarvis_settings table (global Jarvis toggle
list), seed its singleton row, enable Supabase realtime on it, and grant SELECT to
anon/authenticated.

DDL via the Supabase Management API (same approach as
scripts/setup_dashboards_schema.py). Safe to re-run.

Run: python3 scripts/setup_jarvis_schema.py
"""
import os
import re
import sys

import httpx


def _project_ref() -> str:
    ref = os.environ.get("SUPABASE_PROJECT_REF")
    if ref:
        return ref
    url = os.environ.get("SUPABASE_URL", "")
    m = re.match(r"https://([a-z0-9]+)\.supabase\.co", url)
    if m:
        return m.group(1)
    raise SystemExit("SUPABASE_PROJECT_REF not set and could not derive it from SUPABASE_URL")


PROJECT_REF = _project_ref()
ACCESS_TOKEN = os.environ["SUPABASE_ACCESS_TOKEN"]
MGMT = f"https://api.supabase.com/v1/projects/{PROJECT_REF}/database/query"

_HERE = os.path.dirname(os.path.abspath(__file__))
SQL_PATH = os.path.join(_HERE, "..", "migrations", "0004_jarvis.sql")

_TABLES = ["jarvis_settings"]

# Idempotent realtime publication add (ALTER PUBLICATION ... ADD TABLE errors if
# the table is already a member, so guard each with a pg_publication_tables check).
REALTIME_SQL = "\n".join(
    f"""
do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = '{t}'
  ) then
    alter publication supabase_realtime add table public.{t};
  end if;
end $$;
"""
    for t in _TABLES
)


def _run(sql: str, label: str) -> None:
    r = httpx.post(
        MGMT,
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"},
        json={"query": sql},
        timeout=120.0,
    )
    if r.status_code >= 400:
        print(f"[{label}] FAILED HTTP {r.status_code}: {r.text[:500]}", file=sys.stderr)
        r.raise_for_status()
    print(f"[{label}] ok")


if __name__ == "__main__":
    with open(SQL_PATH, encoding="utf-8") as fh:
        ddl = fh.read()
    _run(ddl, "DDL (table + seed + grants)")
    _run(REALTIME_SQL, "realtime publication")
    print("[DONE] jarvis_settings schema ready")
