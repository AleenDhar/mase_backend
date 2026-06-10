"""CLI: verify one or more chats. Used for ad-hoc checks and backfill.

Usage:
    python -m verifier.cli <chat_id> [<chat_id> ...]
        [--project-id PROJECT_ID]
        [--account-token TOKEN ...]
        [--persist]            # also write verifier_report row(s) to Supabase
        [--backfill-abm N]     # verify the N most recent ABM chats

Exit code: 0 if all chats CLEAN (or skipped/no-op), 1 otherwise.
"""

from __future__ import annotations

import argparse
import os
import sys

from supabase import create_client

from .checker import render_verdict
from .flows import ABM_V11_FLOW
from .runner import persist_verdict, run_verifier_for_chat_sync


def _recent_abm_chats(limit: int) -> list[str]:
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    chat_ids: list[str] = []
    for pid in ABM_V11_FLOW.project_ids:
        res = (
            sb.table("chats")
            .select("id, created_at")
            .eq("project_id", pid)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        for row in res.data or []:
            cid = row.get("id")
            if cid:
                chat_ids.append(cid)
    # Preserve order, dedupe
    seen: set[str] = set()
    uniq: list[str] = []
    for cid in chat_ids:
        if cid not in seen:
            seen.add(cid)
            uniq.append(cid)
    return uniq[:limit]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("chat_ids", nargs="*")
    ap.add_argument("--project-id", default=None)
    ap.add_argument("--account-token", action="append", default=[])
    ap.add_argument("--persist", action="store_true")
    ap.add_argument("--backfill-abm", type=int, default=0,
                    help="Verify the N most recent ABM chats")
    args = ap.parse_args(argv)

    chat_ids = list(args.chat_ids)
    if args.backfill_abm > 0:
        chat_ids.extend(_recent_abm_chats(args.backfill_abm))
    if not chat_ids:
        ap.print_help(sys.stderr)
        return 2

    exit_code = 0
    for cid in chat_ids:
        verdict = run_verifier_for_chat_sync(
            cid,
            project_id=args.project_id,
            extra_account_tokens=args.account_token,
        )
        if verdict is None:
            print(f"--- {cid} — skipped (out of scope or no data) ---")
            continue
        print(render_verdict(verdict))
        print()
        if args.persist:
            outcomes = persist_verdict(verdict)
            print(f"  persisted: {outcomes}")
        if not verdict.passed:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
