"""Agent tools for reading Avoma → SF enrichment reports.

Exposes 2 @tool functions to the agent:
    - get_avoma_reports        (list, filterable)
    - get_avoma_report_detail  (full single report w/ 3 tiers + summaries)

Both read from public.avoma_event_reports via Supabase REST. Read-only.
"""

import json
import os
from typing import Optional

import httpx
from langchain_core.tools import tool

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY")
    or ""
)


def _rest(path: str, params: dict) -> list:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError("Supabase not configured (SUPABASE_URL / SERVICE_ROLE_KEY)")
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    r = httpx.get(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=headers,
        params=params,
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


@tool
def get_avoma_reports(
    limit: int = 20,
    status: Optional[str] = None,
    sf_opportunity_id: Optional[str] = None,
    sf_account_id: Optional[str] = None,
    meeting_subject_contains: Optional[str] = None,
) -> str:
    """List Avoma → Salesforce enrichment reports (one row per processed meeting).

    Each report was generated when an Avoma meeting fired a webhook into this
    server and was enriched with 3 tiers of Salesforce data + gpt-4o-mini
    summaries:
      tier 1 = deal_health     (Opp + History + open Tasks + last 5 activities)
      tier 2 = account_briefing (tier 1 + Account + Contacts + related Opps + TeamMember + ContactRole)
      tier 3 = full_snapshot    (tier 2 + LineItems + FieldHistory + Feed + Documents + EmailMessage + Cases + Contracts + Campaigns)

    Use this to find recent reports, or filter by deal / account / status
    before pulling a specific report's full content via get_avoma_report_detail.

    Args:
        limit:                    Max rows (default 20, hard cap 200).
        status:                   Filter: 'completed', 'failed', 'no_sf_links', 'pending'.
        sf_opportunity_id:        Salesforce 15/18-char Opportunity Id.
        sf_account_id:            Salesforce 15/18-char Account Id.
        meeting_subject_contains: Substring (ILIKE) match on meeting_subject.

    Returns:
        JSON string with `count` and `reports[]` (lightweight: message_id,
        meeting_subject, meeting_start_at, status, sf_opportunity_id,
        sf_account_id, created_at, pull_duration_ms).
        Use get_avoma_report_detail(message_id) for full tier data + summaries.
    """
    try:
        params = {
            "select": "message_id,meeting_uuid,meeting_subject,meeting_start_at,"
                      "status,sf_opportunity_id,sf_account_id,created_at,pull_duration_ms",
            "order":  "created_at.desc",
            "limit":  str(min(max(int(limit), 1), 200)),
        }
        if status:                    params["status"] = f"eq.{status}"
        if sf_opportunity_id:         params["sf_opportunity_id"] = f"eq.{sf_opportunity_id}"
        if sf_account_id:             params["sf_account_id"] = f"eq.{sf_account_id}"
        if meeting_subject_contains:  params["meeting_subject"] = f"ilike.*{meeting_subject_contains}*"

        rows = _rest("avoma_event_reports", params)
        return json.dumps({"count": len(rows), "reports": rows}, default=str)
    except Exception as e:
        return json.dumps({"error": f"get_avoma_reports failed: {type(e).__name__}: {e}"})


@tool
def get_avoma_report_detail(message_id: str, include_raw_data: bool = False) -> str:
    """Fetch the full enrichment report for one meeting by message_id.

    Returns all 3 tier SUMMARIES by default (deal_health_summary,
    account_briefing_summary, full_snapshot_summary). Set include_raw_data=True
    to also return the raw jsonb tier payloads (deal_health_data ~5KB,
    account_briefing_data ~30KB, full_snapshot_data ~50-100KB). Most questions
    can be answered from the summaries alone.

    Args:
        message_id:       UUID of the report (from get_avoma_reports).
        include_raw_data: If True, include the raw *_data jsonb blobs.

    Returns:
        JSON string with meeting metadata + the 3 tier summaries, plus the raw
        *_data fields if include_raw_data=True.
    """
    try:
        if include_raw_data:
            select = "*"
        else:
            select = ("message_id,meeting_uuid,meeting_subject,meeting_start_at,"
                      "status,sf_opportunity_id,sf_account_id,sf_contact_ids,"
                      "created_at,processed_at,pull_duration_ms,error,"
                      "deal_health_summary,account_briefing_summary,full_snapshot_summary,"
                      "opportunity_analysis_status,opportunity_analysis_data")
        rows = _rest("avoma_event_reports", {
            "select":     select,
            "message_id": f"eq.{message_id}",
            "limit":      "1",
        })
        if not rows:
            return json.dumps({"error": f"no report found for message_id={message_id}"})
        return json.dumps(rows[0], default=str)
    except Exception as e:
        return json.dumps({"error": f"get_avoma_report_detail failed: {type(e).__name__}: {e}"})
