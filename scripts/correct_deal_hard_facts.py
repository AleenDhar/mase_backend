#!/usr/bin/env python3
"""Deterministically correct the stored deal_records' HARD Salesforce facts — no AI.

The Deal Intelligence sweep is an LLM that emits a canonical record. Its `hard`
block is meant to be Salesforce ground truth, but records produced before the
anti-fabrication hardening can carry model-authored (hallucinated) deterministic
facts: a wrong owner-manager, a made-up competitor, stale AIS, or invented dates.

This script triggers the server's AI-FREE hard-refresh, which re-reads the live
Salesforce values for every persisted opportunity and OVERWRITES the hard facts
(owner/manager/account/name, stage, forecast, amount, close_date, next_step,
products, competitor, AIS, the created/last-modified/last-activity/qualified
dates, and the server-computed days_to_close) while leaving the AI analysis,
packets and history untouched. Every governed fact becomes exactly what
Salesforce says — including null where Salesforce is blank — so a model-invented
value for a blank field is cleared.

We POST to the RUNNING server's `/api/deal-engine/hard-refresh` rather than
importing the engine in a separate process, so the correction shares the server's
in-process mutual-exclusion guard and cached Salesforce connection and can never
clobber a concurrent sweep.

Usage:
    python3 scripts/correct_deal_hard_facts.py [--base URL] [--concurrency N]
                                               [--delete-initial-interest]

Auth: reads API_AUTH_TOKEN (fallback DISPATCH_SECRET) from the environment — the
same token the server's API gate uses. Run it from the same environment as the
server so those vars are present.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Deterministically correct stored deal hard facts from Salesforce (no AI).")
    ap.add_argument(
        "--base", default=os.getenv("DEAL_ENGINE_BASE_URL", "http://localhost:5000"),
        help="Base URL of the running DeepAgent server (default: http://localhost:5000).")
    ap.add_argument(
        "--concurrency", type=int, default=None,
        help="Bulk SOQL/write concurrency (default: server's DEAL_HARD_REFRESH_CONCURRENCY).")
    ap.add_argument(
        "--delete-initial-interest", action="store_true",
        help="Also DELETE deals slipped back to 'Initial Interest' (default: keep them; "
             "this run is a pure data correction, not discovery hygiene).")
    ap.add_argument(
        "--timeout", type=int, default=1800, help="HTTP timeout in seconds (default: 1800).")
    args = ap.parse_args()

    token = os.getenv("API_AUTH_TOKEN") or os.getenv("DISPATCH_SECRET")
    if not token:
        print("WARNING: no API_AUTH_TOKEN / DISPATCH_SECRET in env — sending unauthenticated "
              "(works only if the server's API gate is disabled).", file=sys.stderr)

    body: dict = {"delete_initial_interest": bool(args.delete_initial_interest)}
    if args.concurrency is not None:
        body["concurrency"] = args.concurrency
    data = json.dumps(body).encode("utf-8")

    url = args.base.rstrip("/") + "/api/deal-engine/hard-refresh"
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    print(f"POST {url}  delete_initial_interest={body['delete_initial_interest']}"
          + (f"  concurrency={args.concurrency}" if args.concurrency is not None else ""))
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            payload = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}", file=sys.stderr)
        return 2
    except urllib.error.URLError as e:
        print(f"connection failed ({e}). Is the DeepAgent Server workflow running?",
              file=sys.stderr)
        return 2

    try:
        summary = json.loads(payload)
    except json.JSONDecodeError:
        print(f"HTTP {status}: {payload}")
        return 0

    if summary.get("skipped"):
        print(f"skipped: {summary['skipped']} — a full sweep or another hard refresh is "
              "running. Retry once it finishes.")
        return 1

    print("Correction complete:")
    print(json.dumps(summary, indent=2))
    print(f"\n  records={summary.get('records')}  matched={summary.get('matched')}  "
          f"updated={summary.get('updated')}  removed={summary.get('removed')}  "
          f"unmatched={summary.get('unmatched')}  failed={summary.get('failed')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
