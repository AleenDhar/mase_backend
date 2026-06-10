"""
One-time, idempotent backfill that seeds a living-memory packets baseline onto
every Deal Intelligence record that predates living memory.

Why this exists:
  Living memory (packets + deltas) is created the next time each deal is swept.
  Deals not re-swept since living memory shipped still have no packet store, so
  the new "What changed" feed looks empty for them until their natural next
  sweep. This pass seeds a baseline across the whole book immediately.

What it does (see deal_engine_store.backfill_packets / packets.seed_packets):
  For each record WITHOUT a packet store it derives packets from the record's
  EXISTING ai.*/hard and attaches them with an EMPTY delta log. Seeding
  pre-existing facts is NOT a change, so it emits no `added` deltas (the
  migration is treated as seeding, not history). Prior analysis is preserved
  exactly (ai is not re-projected). schema_version is stamped 2.

Idempotent: records already at schema_version >= 2 are skipped, so re-running is
a no-op and never double-seeds.

Usage:
  python3 scripts/backfill_deal_packets.py [--dry-run]

  --dry-run   Compute and print the stats but write nothing.

Requires env vars: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY (or
SUPABASE_SERVICE_KEY).
"""

import argparse
import os
import sys

# Make root-level modules importable from scripts/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import deal_engine_store as store  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would change; write nothing.")
    args = ap.parse_args()

    print(f"Seeding deal living-memory packets baseline  dry_run={args.dry_run}")
    try:
        stats = store.backfill_packets(dry_run=args.dry_run)
    except store.DealEngineError as e:
        raise SystemExit(f"backfill failed: {e}")
    print(f"Done. {stats}")


if __name__ == "__main__":
    main()
