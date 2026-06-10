"""
Task #10 — Opportunity Diagnosis Data Lake: Historical Backfill

Populates lake.opportunity_diagnoses for the ~83 historical OD chats that
existed before the lake was built.

Source projects:
  87f864e2-50bf-4015-a0f8-4ed7426b2a50  Bite Size 2.0  (~12 chats)
  22fbcc90-f594-4fd3-978c-26b9efeced11  Bite Size v1   (~71 chats)

Usage:
  python3 scripts/backfill_lake.py [--dry-run]

  --dry-run   Discovers chats and prints what would be written, but
              does NOT write any rows to the lake table.

Requires env vars:  SUPABASE_URL, SUPABASE_SERVICE_KEY, OPENAI_API_KEY

The script is idempotent — upsert on (chat_id, run_at) means re-running
it is safe and will not create duplicates.
"""

import asyncio
import os
import sys
from typing import Optional

# Make root-level modules importable from scripts/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supabase import create_client
import lake as _lake

OD_PROJECT_IDS = list(_lake.OD_PROJECT_IDS)

BATCH_SIZE = 50   # max chat_ids per .in_() query


def _chunked(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i: i + n]


def _discover_chats(sb) -> dict[str, list[str]]:
    """Return {project_id: [chat_id, ...]} for all OD projects.

    Queries the `chats` table joining on project_id (written by the
    Next.js frontend).  Falls back to an empty dict if the column does
    not yet exist, logging a clear message.
    """
    result: dict[str, list[str]] = {pid: [] for pid in OD_PROJECT_IDS}
    try:
        resp = (
            sb.table("chats")
            .select("id,project_id")
            .in_("project_id", OD_PROJECT_IDS)
            .execute()
        )
        for row in resp.data or []:
            pid = row.get("project_id")
            cid = row.get("id")
            if pid and cid and pid in result:
                result[pid].append(cid)
    except Exception as e:
        print(f"[BACKFILL] ERROR querying chats table: {e}")
        print("[BACKFILL] Cannot continue — chats.project_id column may not exist yet.")
    return result


def _get_final_message(sb, chat_id: str) -> Optional[dict]:
    """Return the LAST type='final' row for a chat, or None."""
    try:
        resp = (
            sb.table("chat_messages")
            .select("chat_id,content,created_at")
            .eq("chat_id", chat_id)
            .eq("type", "final")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        return rows[0] if rows else None
    except Exception as e:
        raise RuntimeError(f"query chat_messages failed: {e}") from e


async def _process_chat(
    sb,
    chat_id: str,
    project_id: str,
    openai_api_key: str,
    dry_run: bool,
) -> str:
    """Process a single chat.  Returns a one-line status string."""
    try:
        final_row = _get_final_message(sb, chat_id)
        if not final_row:
            return f"SKIP  {chat_id}  (no type=final message)"

        # run_at comes directly from created_at — no parsing layer
        run_at: str = final_row["created_at"]
        final_response: str = final_row.get("content") or ""

        if dry_run:
            preview = final_response[:120].replace("\n", " ")
            return (
                f"DRY     {chat_id}  run_at={run_at}  "
                f"response_len={len(final_response)}  preview={preview!r}"
            )

        # Use backfill writer — raises on failure, signals existing rows
        status, detail = await _lake.write_lake_diagnosis_backfill(
            chat_id=chat_id,
            project_id=project_id,
            final_response=final_response,
            supabase_client=sb,
            openai_api_key=openai_api_key,
            run_at=run_at,
        )
        if status == "exists":
            return f"EXISTS  {chat_id}  (already in lake)"
        return f"WRITTEN {chat_id}  run_at={run_at}  account={detail}"

    except Exception as e:
        first_line = str(e).split("\n")[0][:200]
        return f"FAIL    {chat_id}  {first_line}"


async def run_backfill(dry_run: bool = False) -> None:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    openai_api_key = os.environ.get("OPENAI_API_KEY", "")

    sb = create_client(url, key)
    print(f"[BACKFILL] Connected to Supabase: {url}")
    print(f"[BACKFILL] dry_run={dry_run}")
    print()

    project_chats = _discover_chats(sb)
    total_chats = sum(len(v) for v in project_chats.values())
    print(f"[BACKFILL] Found {total_chats} OD chats across {len(OD_PROJECT_IDS)} projects:")
    for pid, cids in project_chats.items():
        print(f"  {pid}  →  {len(cids)} chats")
    print()

    if total_chats == 0:
        print("[BACKFILL] Nothing to process.  Check that chats.project_id is populated.")
        return

    counters = {"written": 0, "exists": 0, "skip": 0, "fail": 0, "dry": 0}
    failures: list[str] = []

    for project_id, chat_ids in project_chats.items():
        print(f"[BACKFILL] Processing project {project_id} ({len(chat_ids)} chats)")
        for chat_id in chat_ids:
            status = await _process_chat(sb, chat_id, project_id, openai_api_key, dry_run)
            print(f"  {status}")
            prefix = status.split()[0]
            if prefix == "WRITTEN":
                counters["written"] += 1
            elif prefix == "EXISTS":
                counters["exists"] += 1
            elif prefix == "SKIP":
                counters["skip"] += 1
            elif prefix == "FAIL":
                counters["fail"] += 1
                failures.append(status)
            elif prefix == "DRY":
                counters["dry"] += 1
        print()

    print("=" * 60)
    if dry_run:
        print(f"[BACKFILL] DRY RUN COMPLETE — no rows written")
        print(f"  Would process: {counters['dry']}  Skip (no final): {counters['skip']}")
    else:
        print(f"[BACKFILL] DONE")
        print(
            f"  Written: {counters['written']}  "
            f"Already existed: {counters['exists']}  "
            f"Skipped (no final): {counters['skip']}  "
            f"Failed: {counters['fail']}"
        )

    if failures:
        print(f"\n[BACKFILL] Failures ({len(failures)}):")
        for f in failures:
            print(f"  {f}")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    asyncio.run(run_backfill(dry_run=dry_run))
