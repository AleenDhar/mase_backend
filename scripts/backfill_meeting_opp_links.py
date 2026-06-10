"""
Reconnect meeting analyses that aren't linked to a deal.

Repairs the two link columns the /mcp meeting tools read:
  - avoma_event_reports.sf_opportunity_id  (read by get_meeting_analysis)
  - meeting_cache.opportunity_ids          (read by list_opportunity_meetings)

Why links go missing (investigation summary):
  1. sf_opportunity_id is NULL on a row when Avoma's meeting object had no
     "oppo" crm_association at webhook time (no_sf_links / completed_no_opportunity)
     OR the Avoma fetch failed transiently (status=failed, often a 429). The raw
     SNS envelope stored on the row does NOT carry crm_associations, so Avoma's
     /meetings/{uuid}/?include_crm_associations=true is the only source of the
     opp link.
  2. meeting_cache.opportunity_ids was populated only from the SF *pull* result
     (deal_health_data.opportunity[0].Id). If the pull returned no opp row (opp
     deleted, pull error) the array was left empty even though sf_opportunity_id
     was resolved — so the meeting was unfindable by opportunity.

What this script does (idempotent, never raises per-row):
  Pass A (Avoma re-resolution, optional): for reports with a NULL
    sf_opportunity_id, re-fetch the meeting from Avoma. If an opportunity (or
    account) is now associated, update the report. Meetings that 404 in Avoma
    (aged out / deleted) are counted and skipped — they cannot be recovered.
  Pass B (deterministic, no external calls): for every report that has a
    sf_opportunity_id, ensure a meeting_cache row exists with that id in
    opportunity_ids (and account_id set). Fixes the empty/ missing rows.
  Pass C (deterministic): recompute opportunity_cache.meetings_count +
    last_meeting_date from meeting_cache so list_cached_opportunities reflects
    the repaired links.

Usage:
  python3 scripts/backfill_meeting_opp_links.py [--dry-run] [--no-avoma]
                                                [--limit N] [--sleep S]

  --dry-run    Print what would change; write nothing.
  --no-avoma   Skip Pass A (no Avoma calls); only run the deterministic
               cache repair from already-resolved opp ids.
  --limit N    Cap Pass A to N reports (default: all NULL-opp reports).
  --sleep S    Seconds to sleep between Avoma calls (default 0.3).

Requires env vars: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY (or
SUPABASE_SERVICE_KEY), AVOMA_API_TOKEN (for Pass A).
"""

import argparse
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone

# Make root-level modules importable from scripts/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supabase import create_client
import sf_meeting_enricher as enricher


def _sb():
    url = os.environ["SUPABASE_URL"]
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
           or os.environ.get("SUPABASE_SERVICE_KEY"))
    if not (url and key):
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY/"
                         "SUPABASE_SERVICE_KEY must be set")
    return create_client(url, key)


def _parse_dt(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _fetch_all(sb, table, columns):
    """Page through a whole table (supabase caps at 1000 rows/request)."""
    rows, off, page = [], 0, 1000
    while True:
        res = (sb.table(table).select(columns)
               .range(off, off + page - 1).execute())
        chunk = res.data or []
        rows.extend(chunk)
        if len(chunk) < page:
            break
        off += page
    return rows


# ---------------- Pass A: Avoma re-resolution ----------------

def pass_a_avoma(sb, *, dry_run, limit, sleep_s):
    reports = _fetch_all(
        sb, "avoma_event_reports",
        "message_id,meeting_uuid,sf_opportunity_id,sf_account_id,status",
    )
    null_opp = [r for r in reports
                if not r.get("sf_opportunity_id") and r.get("meeting_uuid")]
    if limit:
        null_opp = null_opp[:limit]
    print(f"\n[PASS A] Avoma re-resolution over {len(null_opp)} NULL-opp reports")

    stats = Counter()
    for r in null_opp:
        mu = r["meeting_uuid"]
        try:
            ids = enricher.extract_sf_ids_from_meeting(mu)
        except Exception as e:
            msg = str(e)
            if "404" in msg:
                stats["avoma_404_gone"] += 1
            else:
                stats["avoma_error"] += 1
            time.sleep(sleep_s)
            continue
        opp = ids.get("opportunity_id")
        acct = ids.get("account_id")
        contacts = ids.get("contact_ids") or []
        if not opp:
            stats["still_no_opp"] += 1
            # Backfill a newly-found account even when there's still no opp.
            if acct and not r.get("sf_account_id") and not dry_run:
                sb.table("avoma_event_reports").update(
                    {"sf_account_id": acct}
                ).eq("message_id", r["message_id"]).execute()
                stats["account_only_filled"] += 1
            time.sleep(sleep_s)
            continue
        # Opportunity recovered.
        stats["opp_recovered"] += 1
        print(f"  recovered opp {opp} for meeting {mu[:12]} "
              f"(was {r.get('status')})")
        if not dry_run:
            sb.table("avoma_event_reports").update({
                "sf_opportunity_id": opp,
                "sf_account_id": acct or r.get("sf_account_id"),
                "sf_contact_ids": contacts,
                "status": "completed",
            }).eq("message_id", r["message_id"]).execute()
        time.sleep(sleep_s)

    print(f"[PASS A] {dict(stats)}")
    return stats


# ---------------- Pass B: meeting_cache repair ----------------

def pass_b_meeting_cache(sb, *, dry_run):
    reports = _fetch_all(
        sb, "avoma_event_reports",
        "meeting_uuid,sf_opportunity_id,sf_account_id,meeting_subject,"
        "meeting_start_at,id",
    )
    with_opp = [r for r in reports
                if r.get("sf_opportunity_id") and r.get("meeting_uuid")]
    cache = {r["meeting_uuid"]: r
             for r in _fetch_all(sb, "meeting_cache",
                                 "meeting_uuid,opportunity_ids")}
    print(f"\n[PASS B] meeting_cache repair over {len(with_opp)} "
          f"resolved-opp reports")

    stats = Counter()
    for r in with_opp:
        mu = r["meeting_uuid"]
        opp = r["sf_opportunity_id"]
        existing = cache.get(mu)
        cur_ids = (existing or {}).get("opportunity_ids") or []
        if existing and opp in cur_ids:
            stats["already_linked"] += 1
            continue
        new_ids = sorted(set(cur_ids) | {opp})
        payload = {
            "meeting_uuid": mu,
            "opportunity_ids": new_ids,
            "report_id": r.get("id"),
        }
        # Only set descriptive cols when creating a brand-new row so we never
        # clobber richer data written by the live webhook.
        if not existing:
            payload.update({
                "meeting_title": r.get("meeting_subject"),
                "meeting_date": r.get("meeting_start_at"),
                "account_id": r.get("sf_account_id"),
            })
            stats["row_created"] += 1
        else:
            stats["ids_extended"] += 1
        if not dry_run:
            sb.table("meeting_cache").upsert(
                payload, on_conflict="meeting_uuid").execute()

    print(f"[PASS B] {dict(stats)}")
    return stats


# ---------------- Pass C: opportunity_cache counts ----------------

def pass_c_opp_counts(sb, *, dry_run):
    meetings = _fetch_all(sb, "meeting_cache",
                          "opportunity_ids,meeting_date")
    counts: dict[str, int] = {}
    last_date: dict[str, datetime] = {}
    for m in meetings:
        md = _parse_dt(m.get("meeting_date"))
        for oid in (m.get("opportunity_ids") or []):
            counts[oid] = counts.get(oid, 0) + 1
            if md and (oid not in last_date or md > last_date[oid]):
                last_date[oid] = md

    existing = {r["opportunity_id"]
                for r in _fetch_all(sb, "opportunity_cache", "opportunity_id")}
    print(f"\n[PASS C] recompute meetings_count for "
          f"{len(counts)} opps with meetings")

    stats = Counter()
    for oid, cnt in counts.items():
        if oid not in existing:
            stats["opp_not_in_cache"] += 1
            continue
        ld = last_date.get(oid)
        payload = {
            "opportunity_id": oid,
            "meetings_count": cnt,
            "last_meeting_date": ld.isoformat() if ld else None,
        }
        if not dry_run:
            sb.table("opportunity_cache").upsert(
                payload, on_conflict="opportunity_id").execute()
        stats["opp_updated"] += 1
    print(f"[PASS C] {dict(stats)}")
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-avoma", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=0.3)
    args = ap.parse_args()

    sb = _sb()
    print(f"Backfill meeting↔opp links  dry_run={args.dry_run}  "
          f"avoma={'off' if args.no_avoma else 'on'}")

    if not args.no_avoma:
        pass_a_avoma(sb, dry_run=args.dry_run,
                     limit=args.limit or None, sleep_s=args.sleep)
    pass_b_meeting_cache(sb, dry_run=args.dry_run)
    pass_c_opp_counts(sb, dry_run=args.dry_run)
    print("\nDone.")


if __name__ == "__main__":
    main()
