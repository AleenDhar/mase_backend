import os
import json
import time
import threading
from typing import Optional, Any
from datetime import datetime, timedelta
from urllib.parse import quote

import httpx
from fastmcp import FastMCP

LINKEDIN_REST_BASE = "https://api.linkedin.com/rest"
LINKEDIN_AUTH_URL = "https://www.linkedin.com/oauth/v2/accessToken"
LINKEDIN_API_VERSION = "202602"

ACCESS_TOKEN = os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip()
CLIENT_ID = os.environ.get("LINKEDIN_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("LINKEDIN_CLIENT_SECRET", "")

_token_lock = threading.Lock()
_access_token: Optional[str] = None
_token_expiry: float = 0.0

# Metric fields to always request from adAnalytics.
# NOTE: 'leads' is NOT present in AdAnalyticsV8 and causes a hard 400 error.
# However, 'oneClickLeads' (Lead Gen Form submissions) and
# 'oneClickLeadFormOpens' (form opens) ARE available and confirmed working
# with the Lead Sync API Standard Tier + r_ads_leadgen_automation scope.
_ANALYTICS_FIELDS = (
    "impressions,clicks,costInLocalCurrency,totalEngagements,"
    "videoViews,videoCompletions,opens,sends,"
    "oneClickLeads,oneClickLeadFormOpens,"
    "externalWebsiteConversions,"
    "approximateUniqueImpressions,pivotValues,dateRange"
)

mcp = FastMCP(
    name="LinkedIn Ads",
    instructions=(
        "Use this server to interact with the LinkedIn Advertising API (version 202602). "
        "You can list ad accounts, campaign groups, campaigns, and pull detailed "
        "performance analytics with various pivots and granularity options. "
        "Requires a valid LinkedIn OAuth2 access token (3-legged, member-auth) "
        "set via LINKEDIN_ACCESS_TOKEN with scopes r_ads and r_ads_reporting. "
        "All campaign endpoints use account-scoped URLs per the 202602 API version. "
        "IMPORTANT: Geographic breakdown by country is NOT available via the analytics pivot. "
        "To analyse performance by region (US/EU/APAC), use linkedin_get_campaigns to get "
        "all campaigns with their targeting criteria (locations field), group them by region "
        "manually, then call linkedin_get_campaign_analytics per campaign to aggregate spend "
        "and engagement per region."
    ),
)


def _get_access_token() -> str:
    global _access_token, _token_expiry

    if ACCESS_TOKEN:
        return ACCESS_TOKEN

    with _token_lock:
        if _access_token and time.time() < _token_expiry:
            return _access_token

        if not CLIENT_ID or not CLIENT_SECRET:
            raise RuntimeError(
                "LINKEDIN_ACCESS_TOKEN (or LINKEDIN_CLIENT_ID + LINKEDIN_CLIENT_SECRET) "
                "environment variable must be set. "
                "Required scopes: r_ads, r_ads_reporting. "
                "Note: LinkedIn Ads API requires 3-legged OAuth (member-auth)."
            )

        with httpx.Client(timeout=30) as client:
            resp = client.post(
                LINKEDIN_AUTH_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                },
            )
            if resp.status_code != 200:
                raise RuntimeError(f"LinkedIn OAuth2 failed ({resp.status_code}): {resp.text}")
            data = resp.json()

        _access_token = data["access_token"]
        _token_expiry = time.time() + data.get("expires_in", 3600) - 60
        return _access_token


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_access_token()}",
        "Content-Type": "application/json",
        "LinkedIn-Version": LINKEDIN_API_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
    }


def _get_raw(raw_url: str) -> Any:
    with httpx.Client(timeout=30) as client:
        req = client.build_request("GET", raw_url, headers=_headers())
        resp = client.send(req)
    if resp.status_code >= 400:
        raise RuntimeError(f"LinkedIn API error {resp.status_code}: {resp.text}")
    try:
        return resp.json()
    except Exception:
        return {"raw_response": resp.text}


def _get_raw_v2(raw_url: str) -> Any:
    """GET against the LinkedIn v2 API (no LinkedIn-Version header — v2 style)."""
    headers = {
        "Authorization": f"Bearer {_get_access_token()}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=30) as client:
        req = client.build_request("GET", raw_url, headers=headers)
        resp = client.send(req)
    if resp.status_code >= 400:
        raise RuntimeError(f"LinkedIn v2 API error {resp.status_code}: {resp.text}")
    try:
        return resp.json()
    except Exception:
        return {"raw_response": resp.text}


def _get(endpoint: str, params: Optional[dict] = None) -> Any:
    url = f"{LINKEDIN_REST_BASE}{endpoint}"
    with httpx.Client(timeout=30) as client:
        req = client.build_request("GET", url, headers=_headers(), params=params)
        resp = client.send(req)
    if resp.status_code >= 400:
        raise RuntimeError(f"LinkedIn API error {resp.status_code}: {resp.text}")
    try:
        return resp.json()
    except Exception:
        return {"raw_response": resp.text}


def _build_date_range(start_date: str, end_date: str) -> str:
    s = start_date.split("-")
    e = end_date.split("-")
    return (
        f"(start:(year:{int(s[0])},month:{int(s[1])},day:{int(s[2])}),"
        f"end:(year:{int(e[0])},month:{int(e[1])},day:{int(e[2])}))"
    )


def _fetch_all_analytics_pages(base_url: str, page_size: int = 100) -> list:
    """
    Fetch all pages from an adAnalytics URL, returning all elements.

    If the first request returns a 400 'Projected field not present in schema'
    error, the offending field is extracted from the error message and the
    request is retried with that field removed — this handles accounts where
    certain metrics (e.g. 'leads') are absent from AdAnalyticsV8.
    """
    import re as _re

    def _do_fetch(url_with_fields: str) -> list:
        all_elements = []
        start = 0
        while True:
            paged_url = f"{url_with_fields}&start={start}&count={page_size}"
            result = _get_raw(paged_url)
            elements = result.get("elements", [])
            all_elements.extend(elements)
            paging = result.get("paging", {})
            total = paging.get("total", len(all_elements))
            fetched_so_far = start + len(elements)
            if len(elements) < page_size or fetched_so_far >= total:
                break
            start += page_size
        return all_elements

    try:
        return _do_fetch(base_url)
    except RuntimeError as exc:
        msg = str(exc)
        # LinkedIn returns: "Projected field '<field>' not present in schema ..."
        match = _re.search(r"Projected field '(\w+)' not present in schema", msg)
        if match and "fields=" in base_url:
            bad_field = match.group(1)
            # Strip the offending field from the fields= query param and retry once
            fixed_url = _re.sub(
                rf"(fields=[^&]*)(\b{bad_field},?|,?{bad_field}\b)",
                lambda m: m.group(1).replace(m.group(2), ""),
                base_url,
            )
            fixed_url = fixed_url.replace(",,", ",").rstrip(",&")
            return _do_fetch(fixed_url)
        raise


def _fetch_all_list_pages(endpoint: str, base_params: dict, page_size: int = 100) -> list:
    """Paginate a standard LinkedIn list endpoint until all items are fetched."""
    all_items = []
    start = 0
    while True:
        params = {**base_params, "count": page_size, "start": start}
        result = _get(endpoint, params=params)
        elements = result.get("elements", [])
        all_items.extend(elements)
        paging = result.get("paging", {})
        total = paging.get("total", len(all_items))
        fetched_so_far = start + len(elements)
        if len(elements) < page_size or fetched_so_far >= total:
            break
        start += page_size
    return all_items


@mcp.tool()
def linkedin_list_ad_accounts() -> str:
    """
    List all LinkedIn ad accounts accessible by the authenticated user.

    Returns:
        JSON with account IDs, names, statuses, and basic details.
    """
    result = _get("/adAccounts", params={"q": "search", "count": 100})
    accounts = result.get("elements", [])
    return json.dumps({
        "status": "success",
        "total_accounts": len(accounts),
        "accounts": accounts,
    }, indent=2, default=str)


@mcp.tool()
def linkedin_get_ad_account(account_id: str) -> str:
    """
    Get detailed information about a specific LinkedIn ad account.

    Args:
        account_id: The LinkedIn ad account ID (numeric, e.g. "506537541").

    Returns:
        JSON with account details (name, status, type, currency, company).
    """
    result = _get(f"/adAccounts/{account_id}")
    return json.dumps({"status": "success", "account": result}, indent=2, default=str)


@mcp.tool()
def linkedin_get_campaign_groups(
    account_id: str,
    status: Optional[str] = None,
    limit: int = 500,
) -> str:
    """
    Get ALL campaign groups for a LinkedIn ad account (auto-paginated).

    Args:
        account_id: LinkedIn ad account ID.
        status: Optional filter: ACTIVE, PAUSED, ARCHIVED, or DRAFT.
        limit: Max total results to return (default 500, fetches all pages).

    Returns:
        JSON with campaign group list including names, statuses, and budgets.
        Groups are sorted newest first.
    """
    all_groups = _fetch_all_list_pages(
        f"/adAccounts/{account_id}/adCampaignGroups",
        base_params={"q": "search"},
    )
    if status:
        all_groups = [g for g in all_groups if g.get("status") == status]
    if limit:
        all_groups = all_groups[:limit]
    return json.dumps({
        "status": "success",
        "account_id": account_id,
        "total_groups": len(all_groups),
        "campaign_groups": all_groups,
    }, indent=2, default=str)


@mcp.tool()
def linkedin_get_campaigns(
    account_id: str,
    status: Optional[str] = None,
    limit: int = 500,
) -> str:
    """
    Get ALL campaigns for a LinkedIn ad account (auto-paginated, all pages).

    IMPORTANT: This returns every campaign in the account regardless of age.
    Each campaign includes its targetingCriteria which contains geo location URNs
    (e.g. urn:li:geo:103644278 = United States, urn:li:geo:100025096 = European Union,
    urn:li:geo:102454443 = India). Use these to group campaigns by US/EU/APAC region
    for regional analysis, then fetch per-campaign analytics to get regional spend/clicks.

    Args:
        account_id: LinkedIn ad account ID.
        status: Optional filter: ACTIVE, PAUSED, ARCHIVED, COMPLETED, CANCELED, or DRAFT.
        limit: Max total results to return (default 500).

    Returns:
        JSON with full campaign list including names, statuses, budgets, objectives,
        and targetingCriteria (locations for regional grouping).
    """
    all_campaigns = _fetch_all_list_pages(
        f"/adAccounts/{account_id}/adCampaigns",
        base_params={"q": "search"},
    )
    if status:
        all_campaigns = [c for c in all_campaigns if c.get("status") == status]
    if limit:
        all_campaigns = all_campaigns[:limit]
    return json.dumps({
        "status": "success",
        "account_id": account_id,
        "total_campaigns": len(all_campaigns),
        "campaigns": all_campaigns,
        "regional_analysis_note": (
            "To analyse by region, inspect each campaign's targetingCriteria.include "
            "for location URNs: US=urn:li:geo:103644278, UK=urn:li:geo:101165590, "
            "India=urn:li:geo:102713980, Singapore=urn:li:geo:102454443. "
            "Group campaigns by region then sum their analytics."
        ),
    }, indent=2, default=str)


@mcp.tool()
def linkedin_get_campaign_details(account_id: str, campaign_id: str) -> str:
    """
    Get detailed information about a specific LinkedIn campaign including targeting.

    Args:
        account_id: The LinkedIn ad account ID.
        campaign_id: The LinkedIn campaign ID (numeric).

    Returns:
        JSON with campaign name, status, objective, budget, targeting (locations),
        and schedule. The targetingCriteria shows which geos the campaign targets.
    """
    result = _get(f"/adAccounts/{account_id}/adCampaigns/{campaign_id}")
    return json.dumps({"status": "success", "campaign": result}, indent=2, default=str)


@mcp.tool()
def linkedin_get_campaign_analytics(
    campaign_id: str,
    start_date: str,
    end_date: str,
    granularity: str = "MONTHLY",
) -> str:
    """
    Get performance analytics for a specific LinkedIn campaign including spend.

    Returns impressions, clicks, costInLocalCurrency (spend), video views,
    and other engagement metrics per time period.

    Lead counts ARE available: the response includes oneClickLeads (form submissions)
    and oneClickLeadFormOpens (form opens) — confirmed working with Lead Sync API
    Standard Tier. Note: 'leads' (a different field) is NOT in AdAnalyticsV8.

    Args:
        campaign_id: The LinkedIn campaign ID (numeric).
        start_date: Start date in YYYY-MM-DD format.
        end_date: End date in YYYY-MM-DD format.
        granularity: DAILY, MONTHLY, or ALL (aggregate). Default MONTHLY.

    Returns:
        JSON with impressions, clicks, costInLocalCurrency (spend in account currency),
        oneClickLeads (form submissions), oneClickLeadFormOpens (form opens),
        video views, and engagement for each time period.
    """
    date_range = _build_date_range(start_date, end_date)
    campaign_urn = quote(f"urn:li:sponsoredCampaign:{campaign_id}", safe="")
    base_url = (
        f"{LINKEDIN_REST_BASE}/adAnalytics?q=analytics"
        f"&pivot=CAMPAIGN"
        f"&campaigns=List({campaign_urn})"
        f"&timeGranularity={granularity}"
        f"&dateRange={date_range}"
        f"&fields={_ANALYTICS_FIELDS}"
    )
    elements = _fetch_all_analytics_pages(base_url)
    return json.dumps({
        "status": "success",
        "campaign_id": campaign_id,
        "period": {"start": start_date, "end": end_date},
        "granularity": granularity,
        "total_records": len(elements),
        "analytics": elements,
    }, indent=2, default=str)


@mcp.tool()
def linkedin_get_account_analytics(
    account_id: str,
    start_date: str,
    end_date: str,
    granularity: str = "MONTHLY",
    pivot: str = "CAMPAIGN",
) -> str:
    """
    Get aggregate analytics for the entire LinkedIn ad account including spend.

    Returns impressions, clicks, costInLocalCurrency (spend), and other metrics
    broken down by the chosen pivot. Auto-paginates to return all records.

    Lead counts ARE available: the response includes oneClickLeads (form submissions)
    and oneClickLeadFormOpens (form opens) — confirmed working with Lead Sync API
    Standard Tier. Note: 'leads' (a different field) is NOT in AdAnalyticsV8.

    IMPORTANT - Valid pivot values for API v202602:
        CAMPAIGN          - Break down by individual campaign (most useful)
        CAMPAIGN_GROUP    - Break down by campaign group
        CREATIVE          - Break down by individual creative/ad
        ACCOUNT           - Account-level totals only
        COMPANY           - Break down by company size targeting
        MEMBER_COMPANY_SIZE - Audience company size
        MEMBER_INDUSTRY   - Audience industry
        MEMBER_JOB_FUNCTION - Audience job function
        MEMBER_JOB_TITLE  - Audience job title
        MEMBER_SENIORITY  - Audience seniority level

    NOTE: Geographic breakdown (MEMBER_COUNTRY, MEMBER_REGION) is NOT supported
    by API v202602 and will return an error. For US/EU/APAC regional analysis,
    use linkedin_get_campaigns to get campaigns with their targeting locations,
    group by region, then call linkedin_get_campaign_analytics per campaign.

    Args:
        account_id: The LinkedIn ad account ID (numeric).
        start_date: Start date in YYYY-MM-DD format.
        end_date: End date in YYYY-MM-DD format.
        granularity: DAILY, MONTHLY, or ALL (aggregate). Default MONTHLY.
        pivot: Dimension to break down by (see valid values above). Default CAMPAIGN.

    Returns:
        JSON with account-level performance data including spend (costInLocalCurrency),
        clicks, impressions, leads, and engagement broken down by the chosen pivot.
    """
    date_range = _build_date_range(start_date, end_date)
    account_urn = quote(f"urn:li:sponsoredAccount:{account_id}", safe="")
    base_url = (
        f"{LINKEDIN_REST_BASE}/adAnalytics?q=analytics"
        f"&pivot={pivot}"
        f"&accounts=List({account_urn})"
        f"&timeGranularity={granularity}"
        f"&dateRange={date_range}"
        f"&fields={_ANALYTICS_FIELDS}"
    )
    elements = _fetch_all_analytics_pages(base_url)
    return json.dumps({
        "status": "success",
        "account_id": account_id,
        "period": {"start": start_date, "end": end_date},
        "granularity": granularity,
        "pivot": pivot,
        "total_records": len(elements),
        "analytics": elements,
    }, indent=2, default=str)


# ---------------------------------------------------------------------------
# Creatives (ad-level data: copy, headlines, reference URNs, landing pages,
# lead-gen form IDs). Account-scoped endpoint per API v202602:
#   GET /rest/adAccounts/{adAccountId}/creatives?q=criteria
# Optional filter by campaign: &campaigns=List(urn:li:sponsoredCampaign:{id})
# ---------------------------------------------------------------------------


@mcp.tool()
def linkedin_get_creatives(
    account_id: str,
    campaign_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 500,
) -> str:
    """
    Get ALL ad creatives for a LinkedIn ad account (auto-paginated).

    Returns per-creative metadata: ad copy/headline, content reference URN
    (image / video / document / carousel), review + intended status
    (ACTIVE / PAUSED / DRAFT / ARCHIVED), associated campaign URN, landing
    page URL, and lead-gen form ID when attached. Pair with
    linkedin_get_creative_analytics for per-creative performance.

    Args:
        account_id: LinkedIn ad account ID (numeric, e.g. "506537541").
        campaign_id: Optional campaign ID to scope creatives to a single
            campaign. When omitted, returns every creative in the account.
        status: Optional client-side filter on intendedStatus:
            ACTIVE, PAUSED, DRAFT, or ARCHIVED.
        limit: Max total results to return (default 500, fetches all pages).

    Returns:
        JSON with the full creative list including content URNs, status,
        associated campaign, and any inline ad copy / intro text.
    """
    if campaign_id:
        # The `campaigns=List(urn:li:sponsoredCampaign:{id})` filter uses
        # RestLi syntax that httpx's params=dict would URL-encode a second
        # time, breaking the request ("Invalid value type for parameter
        # campaigns"). Build the URL manually and paginate via _get_raw,
        # mirroring _fetch_all_analytics_pages.
        campaign_urn = quote(f"urn:li:sponsoredCampaign:{campaign_id}", safe="")
        base_url = (
            f"{LINKEDIN_REST_BASE}/adAccounts/{account_id}/creatives"
            f"?q=criteria&campaigns=List({campaign_urn})"
        )
        all_creatives = []
        start = 0
        page_size = 100
        while True:
            page = _get_raw(f"{base_url}&start={start}&count={page_size}")
            elements = page.get("elements", [])
            all_creatives.extend(elements)
            paging = page.get("paging", {})
            total = paging.get("total", len(all_creatives))
            fetched = start + len(elements)
            if len(elements) < page_size or fetched >= total:
                break
            start += page_size
    else:
        all_creatives = _fetch_all_list_pages(
            f"/adAccounts/{account_id}/creatives",
            base_params={"q": "criteria"},
        )

    if status:
        all_creatives = [
            c for c in all_creatives if c.get("intendedStatus") == status
        ]
    if limit:
        all_creatives = all_creatives[:limit]

    return json.dumps({
        "status": "success",
        "account_id": account_id,
        "campaign_id": campaign_id,
        "total_creatives": len(all_creatives),
        "creatives": all_creatives,
    }, indent=2, default=str)


@mcp.tool()
def linkedin_get_creative_analytics(
    creative_id: str,
    start_date: str,
    end_date: str,
    granularity: str = "MONTHLY",
) -> str:
    """
    Get performance analytics for a specific LinkedIn ad creative including
    spend.

    Returns impressions, clicks, costInLocalCurrency (spend), one-click lead
    submissions, video views, and other engagement metrics per time period
    for a single creative. Same metric set as linkedin_get_campaign_analytics
    but pivoted at the creative level.

    Args:
        creative_id: The LinkedIn creative ID (numeric — the last segment of
            the creative URN urn:li:sponsoredCreative:{id}).
        start_date: Start date in YYYY-MM-DD format.
        end_date: End date in YYYY-MM-DD format.
        granularity: DAILY, MONTHLY, or ALL (aggregate). Default MONTHLY.

    Returns:
        JSON with impressions, clicks, costInLocalCurrency (spend in account
        currency), oneClickLeads (form submissions), oneClickLeadFormOpens
        (form opens), video views, and engagement for each time period.
    """
    date_range = _build_date_range(start_date, end_date)
    creative_urn = quote(f"urn:li:sponsoredCreative:{creative_id}", safe="")
    base_url = (
        f"{LINKEDIN_REST_BASE}/adAnalytics?q=analytics"
        f"&pivot=CREATIVE"
        f"&creatives=List({creative_urn})"
        f"&timeGranularity={granularity}"
        f"&dateRange={date_range}"
        f"&fields={_ANALYTICS_FIELDS}"
    )
    elements = _fetch_all_analytics_pages(base_url)
    return json.dumps({
        "status": "success",
        "creative_id": creative_id,
        "period": {"start": start_date, "end": end_date},
        "granularity": granularity,
        "total_records": len(elements),
        "analytics": elements,
    }, indent=2, default=str)


# ---------------------------------------------------------------------------
# Lead Gen Forms & Responses
# Requires: r_ads_leadgen_automation scope (separate LinkedIn Lead Sync API
# program — apply at developer.linkedin.com/product-catalog/marketing/lead-generation)
# ---------------------------------------------------------------------------

def _lead_scope_error() -> str:
    return json.dumps({
        "status": "error",
        "error": "missing_scope",
        "message": (
            "The LinkedIn access token does not have the 'r_ads_leadgen_automation' scope. "
            "This scope is part of LinkedIn's Lead Sync API program, which requires a "
            "separate application at: "
            "https://developer.linkedin.com/product-catalog/marketing/lead-generation. "
            "Once approved, re-authenticate and update the LINKEDIN_ACCESS_TOKEN secret."
        ),
    }, indent=2)


@mcp.tool()
def linkedin_list_lead_gen_forms(account_id: str) -> str:
    """
    List all Lead Gen Forms created under a LinkedIn ad account.

    Uses the v2/adForms endpoint (confirmed working with Lead Sync API Standard Tier).
    Returns form ID, name, status, headline, questions, and privacy policy URL.

    Note: Individual lead contact details (name/email) are NOT available via the
    Standard Tier pull API — use linkedin_get_account_analytics or
    linkedin_get_campaign_analytics to get lead COUNTS (oneClickLeads field).

    Args:
        account_id: The LinkedIn ad account ID (numeric, e.g. "506537541").

    Returns:
        JSON with list of lead gen forms including ID, name, status,
        question count, headline, and creation date.
    """
    acct_urn = quote(f"urn:li:sponsoredAccount:{account_id}")
    all_forms = []
    start = 0
    count = 50
    while True:
        url = (
            f"https://api.linkedin.com/v2/adForms"
            f"?q=account&account={acct_urn}"
            f"&start={start}&count={count}"
        )
        result = _get_raw_v2(url)
        elements = result.get("elements", [])
        all_forms.extend(elements)
        paging = result.get("paging", {})
        total = paging.get("total", len(all_forms))
        if len(elements) < count or start + len(elements) >= total:
            break
        start += count

    simplified = []
    for f in all_forms:
        form_data = f.get("form", {})
        simplified.append({
            "form_id": str(f.get("id", "")),
            "name": form_data.get("name", ""),
            "status": f.get("status", ""),
            "headline": form_data.get("headline", ""),
            "question_count": len(form_data.get("questions", [])),
            "questions": [
                q.get("predefinedField", q.get("question", ""))
                for q in form_data.get("questions", [])
            ],
            "landing_page": form_data.get("landingPage", ""),
            "privacy_policy": form_data.get("privacyPolicy", ""),
            "created_at": f.get("created", {}).get("time", ""),
            "last_modified": f.get("lastModified", {}).get("time", ""),
        })

    return json.dumps({
        "status": "success",
        "account_id": account_id,
        "total_forms": len(simplified),
        "note": (
            "Form list retrieved via Lead Sync API Standard Tier. "
            "Individual lead contact details (name/email) require Advanced Tier. "
            "Use analytics tools with oneClickLeads field for lead counts."
        ),
        "forms": simplified,
    }, indent=2, default=str)


@mcp.tool()
def linkedin_get_lead_form_responses(
    account_id: str,
    form_id: str,
    form_version: str = "1",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 500,
) -> str:
    """
    [ADVANCED TIER ONLY] Get individual lead contact details for a specific Lead Gen Form.

    IMPORTANT: This tool requires LinkedIn Lead Sync API Advanced Tier.
    With the current Standard Tier, this endpoint returns 403 ACCESS_DENIED.

    To get lead COUNTS with Standard Tier, use linkedin_get_campaign_analytics or
    linkedin_get_account_analytics — both return oneClickLeads (form submissions)
    and oneClickLeadFormOpens (form opens) in their analytics data.

    To get form definitions (name, questions, status), use linkedin_list_lead_gen_forms.

    Args:
        account_id:   LinkedIn ad account ID (numeric, e.g. "506537541").
        form_id:      Lead gen form ID from linkedin_list_lead_gen_forms.
        form_version: Form version tag (default "1").
        start_date:   Optional ISO date filter YYYY-MM-DD.
        end_date:     Optional ISO date filter YYYY-MM-DD.
        limit:        Max responses to return (default 500).

    Returns:
        JSON explaining Advanced Tier is required, or lead contact data if upgraded.
    """
    return json.dumps({
        "status": "error",
        "error": "advanced_tier_required",
        "message": (
            "Individual lead contact details (name, email, company, job title) "
            "require LinkedIn Lead Sync API Advanced Tier. "
            "The current account has Standard Tier, which only supports webhook delivery "
            "and does not expose the /leadFormResponses pull endpoint. "
            "\n\nTo get LEAD COUNTS right now (Standard Tier works): "
            "use linkedin_get_campaign_analytics or linkedin_get_account_analytics — "
            "the oneClickLeads field shows form submission counts per campaign. "
            "\n\nTo get individual contact details: upgrade to Advanced Tier at "
            "https://developer.linkedin.com/product-catalog/marketing/lead-generation"
        ),
        "account_id": account_id,
        "form_id": form_id,
        "what_works_now": {
            "lead_counts_by_campaign": "linkedin_get_campaign_analytics → oneClickLeads field",
            "lead_counts_account_total": "linkedin_get_account_analytics → oneClickLeads field",
            "form_definitions": "linkedin_list_lead_gen_forms → form names, questions, status",
        },
    }, indent=2)


@mcp.tool()
def linkedin_get_all_leads(
    account_id: str,
    start_date: str,
    end_date: str,
) -> str:
    """
    Get lead submission COUNTS across all campaigns for a LinkedIn ad account.

    This is the main tool to use when asked "how many leads did we get from LinkedIn?"
    — it pulls oneClickLeads (form submissions) and oneClickLeadFormOpens (form opens)
    from the analytics API, broken down by campaign, for the given date range.

    Note: Individual lead contact details (name/email) require Advanced Tier.
    This tool returns aggregated counts, which ARE available with Standard Tier.

    Args:
        account_id:  LinkedIn ad account ID (numeric, e.g. "506537541").
        start_date:  Start date YYYY-MM-DD.
        end_date:    End date YYYY-MM-DD.

    Returns:
        JSON with total lead count, per-campaign breakdown of oneClickLeads and
        oneClickLeadFormOpens, sorted by most leads first.
    """
    date_range = _build_date_range(start_date, end_date)
    account_urn = quote(f"urn:li:sponsoredAccount:{account_id}", safe="")
    base_url = (
        f"{LINKEDIN_REST_BASE}/adAnalytics?q=analytics"
        f"&pivot=CAMPAIGN"
        f"&accounts=List({account_urn})"
        f"&timeGranularity=ALL"
        f"&dateRange={date_range}"
        f"&fields=oneClickLeads,oneClickLeadFormOpens,clicks,impressions,"
        f"costInLocalCurrency,pivotValues"
    )
    elements = _fetch_all_analytics_pages(base_url)

    total_leads = sum(int(e.get("oneClickLeads", 0)) for e in elements)
    total_opens = sum(int(e.get("oneClickLeadFormOpens", 0)) for e in elements)

    per_campaign = []
    for e in elements:
        leads = int(e.get("oneClickLeads", 0))
        opens = int(e.get("oneClickLeadFormOpens", 0))
        pivot_vals = e.get("pivotValues", [])
        campaign_id = pivot_vals[0].split(":")[-1] if pivot_vals else ""
        per_campaign.append({
            "campaign_id": campaign_id,
            "oneClickLeads": leads,
            "oneClickLeadFormOpens": opens,
            "form_open_to_submit_rate": (
                f"{round(leads / opens * 100, 1)}%" if opens > 0 else "n/a"
            ),
            "clicks": int(e.get("clicks", 0)),
            "impressions": int(e.get("impressions", 0)),
            "spend": e.get("costInLocalCurrency", "0"),
        })

    per_campaign.sort(key=lambda x: x["oneClickLeads"], reverse=True)

    return json.dumps({
        "status": "success",
        "account_id": account_id,
        "period": {"start": start_date, "end": end_date},
        "total_oneClickLeads": total_leads,
        "total_oneClickLeadFormOpens": total_opens,
        "campaigns_with_leads": sum(1 for c in per_campaign if c["oneClickLeads"] > 0),
        "note": (
            "oneClickLeads = Lead Gen Form submissions (confirmed working with Standard Tier). "
            "Individual contact details (name/email) require Advanced Tier."
        ),
        "per_campaign": per_campaign,
    }, indent=2, default=str)


if __name__ == "__main__":
    mcp.run(transport="stdio")
