"""One-off / idempotent: add the membership columns (active, removed_at) +
index to deal_records, so the MASE report can be the single source of truth for
Deal Engine book membership.

DDL via the Supabase Management API (same approach as
scripts/setup_deal_engine_schema.py). Safe to re-run.

Run: python3 scripts/setup_deal_records_membership_schema.py
"""
import os
import sys

import httpx

PROJECT_REF = os.environ["SUPABASE_PROJECT_REF"]
ACCESS_TOKEN = os.environ["SUPABASE_ACCESS_TOKEN"]
MGMT = f"https://api.supabase.com/v1/projects/{PROJECT_REF}/database/query"

_HERE = os.path.dirname(os.path.abspath(__file__))
SQL_PATH = os.path.join(_HERE, "..", "migrations", "0008_deal_records_membership.sql")


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
    _run(ddl, "DDL (deal_records membership columns + index)")
    print("[DONE] deal_records membership schema ready")
