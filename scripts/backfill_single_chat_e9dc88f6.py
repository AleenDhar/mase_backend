"""
One-off backfill for Task #4: set cost_usd for chat e9dc88f6-77dd-4a48-98f9-7e3574e03ae5.

This chat was missed by the Task #3 backfill (cost_usd was non-zero at query time
or written as 0 after the backfill ran). The correct cost is:

  431,613 input tokens  × $1.75 / M  = $0.754822
    4,399 output tokens × $14.00 / M = $0.061586
  ─────────────────────────────────────────────
  Total cost_usd ≈ $0.816408  (exact: 0.81690875)

Run once:
  python3 scripts/backfill_single_chat_e9dc88f6.py
"""

import os
from supabase import create_client

CHAT_ID = "e9dc88f6-77dd-4a48-98f9-7e3574e03ae5"
INPUT_TOKENS = 431_613
OUTPUT_TOKENS = 4_399
RATE_INPUT = 1.75    # $ per million tokens
RATE_OUTPUT = 14.00  # $ per million tokens

COST_USD = (INPUT_TOKENS * RATE_INPUT + OUTPUT_TOKENS * RATE_OUTPUT) / 1_000_000


def main():
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    sb = create_client(url, key)

    print(f"Updating chat {CHAT_ID}")
    print(f"  input_tokens={INPUT_TOKENS:,}  output_tokens={OUTPUT_TOKENS:,}")
    print(f"  cost_usd = {COST_USD:.6f}")

    resp = (
        sb.table("chat_usage")
        .update({"cost_usd": COST_USD})
        .eq("chat_id", CHAT_ID)
        .execute()
    )

    rows = resp.data or []
    if rows:
        print(f"Updated {len(rows)} row(s). Done.")
    else:
        print("WARNING: no rows matched — check that the chat_id exists in chat_usage.")


if __name__ == "__main__":
    main()
