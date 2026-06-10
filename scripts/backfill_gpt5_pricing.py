"""
One-off backfill for Task #3: recalculate cost_usd for chat_usage rows
that were recorded as $0.00 because gpt-5.2 and gpt-5-mini were absent
from _LLM_PRICING in server.py.

Multi-model rows (model field contains a comma) are also updated using the
rate of the first matching gpt-5.2 / gpt-5-mini substring found in the
model string — a deterministic policy since per-model token breakdowns
are not stored in chat_usage.

Pricing rates (per million tokens, matching server.py _LLM_PRICING):
  gpt-5.2    : $1.75 input  / $14.00 output
  gpt-5-mini : $0.15 input  / $0.60  output

Run once after deploying the server.py pricing fix:
  python3 scripts/backfill_gpt5_pricing.py
"""

import os
from supabase import create_client

BACKFILL_TARGETS = [
    ("gpt-5.2",    1.75, 14.00),
    ("gpt-5-mini", 0.15,  0.60),
]


def pick_rate(model: str):
    m = model.lower()
    for key, rate_in, rate_out in BACKFILL_TARGETS:
        if key in m:
            return key, rate_in, rate_out
    return None, None, None


def main():
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    sb = create_client(url, key)

    seen_ids = set()
    total_updated = 0

    for model_key, rate_in, rate_out in BACKFILL_TARGETS:
        resp = (
            sb.table("chat_usage")
            .select("chat_id,model,input_tokens,output_tokens")
            .eq("cost_usd", 0)
            .ilike("model", f"%{model_key}%")
            .execute()
        )
        rows = resp.data or []
        print(f"\n[{model_key}] {len(rows)} zero-cost row(s) found.")

        for row in rows:
            cid = row["chat_id"]
            if cid in seen_ids:
                continue
            seen_ids.add(cid)

            model_str = row.get("model") or ""
            matched_key, r_in, r_out = pick_rate(model_str)
            if matched_key is None:
                continue

            inp = row.get("input_tokens") or 0
            out = row.get("output_tokens") or 0
            cost = (inp * r_in + out * r_out) / 1_000_000

            sb.table("chat_usage") \
              .update({"cost_usd": cost}) \
              .eq("chat_id", cid) \
              .execute()

            print(f"  [{matched_key}] {cid}  inp={inp:,} out={out:,} -> ${cost:.6f}")
            total_updated += 1

    print(f"\nTotal updated: {total_updated}")


if __name__ == "__main__":
    main()
