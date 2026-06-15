import os
import json
import time
import threading
from typing import Optional, Any
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse

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
        "and engagement per region.\n"
        "AD LIBRARY (public ad-transparency, separate from the campaign tools above): "
        "use linkedin_search_ad_library to see the ads ANY company is publicly running — a "
        "competitor's OR our own — by advertiser/company name and/or keyword, optionally "
        "narrowed by country and date range. Use the campaign-analytics tools "
        "(linkedin_get_*_analytics) for OUR OWN spend/clicks/leads performance; use the Ad "
        "Library tools for the actual creative/messaging any advertiser is running publicly. "
        "The search returns ad METADATA (advertiser, format, active dates, impression range, "
        "targeting/countries, public ad_url); the creative COPY (headline/body/CTA) comes "
        "from include_creative=True on the search OR linkedin_get_ad_library_ad(ad_id) for one "
        "ad. Note the destination/landing URL is NOT exposed by LinkedIn's public Ad Library. "
        "For an 'us vs. competitors, how do we improve' request, there is no comparison "
        "endpoint: run linkedin_search_ad_library once per company (ours and each competitor) "
        "with include_creative=True, then reason over the results yourself and give a "
        "side-by-side read with concrete creative/messaging suggestions."
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
# Write tools: pause / reactivate campaigns and creatives
# Require a token with the rw_ads scope AND an ACCOUNT_MANAGER /
# CAMPAIGN_MANAGER / CREATIVE_MANAGER role on the ad account.
# ---------------------------------------------------------------------------

CAMPAIGN_STATUS_VALUES = (
    "ACTIVE", "PAUSED", "ARCHIVED", "DRAFT", "COMPLETED", "CANCELED",
)
CREATIVE_STATUS_VALUES = (
    "ACTIVE", "PAUSED", "ARCHIVED", "CANCELLED", "DRAFT",
)


def _post_partial_update_raw(raw_url: str, patch_body: dict) -> httpx.Response:
    """
    Send a LinkedIn RestLi PARTIAL_UPDATE.

    Uses build_request/send against a fully-built URL (mirroring _get_raw) so an
    already-encoded URN in the path is not double-encoded, and adds the
    X-RestLi-Method: PARTIAL_UPDATE header LinkedIn requires for $set patches.
    Returns the raw response; the caller inspects status_code.
    """
    headers = {**_headers(), "X-RestLi-Method": "PARTIAL_UPDATE"}
    with httpx.Client(timeout=30) as client:
        req = client.build_request("POST", raw_url, headers=headers, json=patch_body)
        resp = client.send(req)
    return resp


def _write_error_payload(resp: httpx.Response, action: str) -> dict:
    """Map a failed LinkedIn write response to a plain-language error dict."""
    status_code = resp.status_code
    raw_text = resp.text or ""
    li_message = ""
    li_code = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            li_message = str(body.get("message", "") or "")
            li_code = str(body.get("code", "") or body.get("serviceErrorCode", "") or "")
    except Exception:
        li_message = raw_text

    lower = f"{li_message} {li_code} {raw_text}".lower()
    li_text = li_message or raw_text

    if "review" in lower and "creative" in action:
        error = "creative_in_review"
        message = (
            "This creative is still in review and cannot be paused or have its "
            "status changed until LinkedIn finishes reviewing it. "
            f"LinkedIn said: {li_text}"
        )
    elif status_code == 426 or "nonexistent_version" in lower:
        error = "api_version_expired"
        message = (
            f"The LinkedIn API version ({LINKEDIN_API_VERSION}) is expired or no "
            "longer accepted. Bump LINKEDIN_API_VERSION in linkedin_mcp_server.py "
            f"to a currently-supported month. LinkedIn said: {li_text}"
        )
    elif status_code == 429 or "rate" in lower and "limit" in lower:
        error = "rate_limited"
        message = (
            "LinkedIn rate-limited this write (429). Wait a moment and retry. "
            f"LinkedIn said: {li_text}"
        )
    elif status_code in (401, 403) or "permission" in lower or "scope" in lower \
            or "access_denied" in lower or "not authorized" in lower:
        error = "permission_or_scope"
        message = (
            f"LinkedIn rejected the {action} ({status_code}). The access token most "
            "likely lacks the 'rw_ads' write scope (read-only 'r_ads' cannot write), "
            "or the token's member is not ACCOUNT_MANAGER / CAMPAIGN_MANAGER / "
            "CREATIVE_MANAGER on this ad account (a VIEWER cannot write even with "
            "rw_ads). A user-provided 3-legged OAuth token with rw_ads is required — "
            f"this cannot be minted automatically. LinkedIn said: {li_text}"
        )
    else:
        error = "linkedin_error"
        message = f"LinkedIn API error during {action} ({status_code}): {li_text}"

    return {
        "status": "error",
        "error": error,
        "http_status": status_code,
        "linkedin_code": li_code,
        "message": message,
    }


@mcp.tool()
def linkedin_set_campaign_status(account_id: str, campaign_id: str, status: str) -> str:
    """
    Pause, reactivate, or otherwise change the status of a LinkedIn campaign.

    This is a WRITE operation that changes live ad delivery: setting a campaign
    to PAUSED stops it serving; setting it to ACTIVE resumes delivery. Requires a
    LINKEDIN_ACCESS_TOKEN with the 'rw_ads' scope and an ACCOUNT_MANAGER /
    CAMPAIGN_MANAGER role on the account.

    IMPORTANT: Resolve the exact numeric account_id and campaign_id first via
    linkedin_get_campaigns / linkedin_get_campaign_details. Never guess a
    campaign from a fuzzy name match — act only on an explicit numeric ID.

    Args:
        account_id: LinkedIn ad account ID (numeric, e.g. "506537541").
        campaign_id: LinkedIn campaign ID (numeric — the last segment of
            urn:li:sponsoredCampaign:{id}).
        status: New status. One of ACTIVE, PAUSED, ARCHIVED, DRAFT, COMPLETED,
            CANCELED.

    Returns:
        JSON with the requested status and the actual current_status re-read
        from LinkedIn after the write (so the result is confirmed, not assumed),
        or a plain-language error (permission/scope, expired version, rate limit).
    """
    status = (status or "").strip().upper()
    if status not in CAMPAIGN_STATUS_VALUES:
        return json.dumps({
            "status": "error",
            "error": "invalid_status",
            "message": (
                f"Invalid campaign status '{status}'. Must be one of: "
                f"{', '.join(CAMPAIGN_STATUS_VALUES)}."
            ),
        }, indent=2)

    raw_url = f"{LINKEDIN_REST_BASE}/adAccounts/{account_id}/adCampaigns/{campaign_id}"
    patch_body = {"patch": {"$set": {"status": status}}}
    try:
        resp = _post_partial_update_raw(raw_url, patch_body)
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "error": "request_failed",
            "message": f"Request to LinkedIn failed: {exc}",
        }, indent=2)

    if resp.status_code >= 400:
        return json.dumps(
            _write_error_payload(resp, f"campaign {campaign_id} status update"),
            indent=2,
        )

    try:
        current = _get(f"/adAccounts/{account_id}/adCampaigns/{campaign_id}")
        new_status = current.get("status")
    except Exception as exc:
        return json.dumps({
            "status": "success",
            "warning": f"Write succeeded but re-read failed: {exc}",
            "account_id": account_id,
            "campaign_id": campaign_id,
            "requested_status": status,
        }, indent=2, default=str)

    return json.dumps({
        "status": "success",
        "account_id": account_id,
        "campaign_id": campaign_id,
        "requested_status": status,
        "current_status": new_status,
        "confirmed": new_status == status,
        "campaign": current,
    }, indent=2, default=str)


@mcp.tool()
def linkedin_set_creative_status(
    account_id: str, creative_urn: str, intended_status: str
) -> str:
    """
    Pause, reactivate, or otherwise change the intendedStatus of a LinkedIn ad
    creative.

    This is a WRITE operation affecting live delivery: setting intendedStatus to
    PAUSED stops the ad serving; ACTIVE resumes it. A creative still in review
    cannot be changed until LinkedIn finishes its review. Requires a
    LINKEDIN_ACCESS_TOKEN with the 'rw_ads' scope and an ACCOUNT_MANAGER /
    CREATIVE_MANAGER role on the account.

    IMPORTANT: Resolve the exact creative via linkedin_get_creatives first. Never
    guess a creative from a fuzzy name match — act only on an explicit ID/URN.

    Args:
        account_id: LinkedIn ad account ID (numeric, e.g. "506537541").
        creative_urn: The creative URN (urn:li:sponsoredCreative:{id}) OR the
            bare numeric creative ID — both are accepted and normalized.
        intended_status: New intendedStatus. One of ACTIVE, PAUSED, ARCHIVED,
            CANCELLED, DRAFT.

    Returns:
        JSON with the requested status and the actual current_intended_status
        re-read from LinkedIn after the write (so the result is confirmed, not
        assumed), or a plain-language error (creative-in-review, permission/scope,
        expired version, rate limit).
    """
    intended_status = (intended_status or "").strip().upper()
    if intended_status not in CREATIVE_STATUS_VALUES:
        return json.dumps({
            "status": "error",
            "error": "invalid_status",
            "message": (
                f"Invalid creative intendedStatus '{intended_status}'. Must be one "
                f"of: {', '.join(CREATIVE_STATUS_VALUES)}."
            ),
        }, indent=2)

    raw = (creative_urn or "").strip()
    if raw.startswith("urn:li:sponsoredCreative:"):
        creative_id = raw.split(":")[-1]
    else:
        creative_id = raw
    if not creative_id:
        return json.dumps({
            "status": "error",
            "error": "invalid_creative",
            "message": "creative_urn is required (a URN or a bare numeric ID).",
        }, indent=2)

    full_urn = f"urn:li:sponsoredCreative:{creative_id}"
    encoded = quote(full_urn, safe="")
    raw_url = f"{LINKEDIN_REST_BASE}/adAccounts/{account_id}/creatives/{encoded}"
    patch_body = {"patch": {"$set": {"intendedStatus": intended_status}}}
    try:
        resp = _post_partial_update_raw(raw_url, patch_body)
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "error": "request_failed",
            "message": f"Request to LinkedIn failed: {exc}",
        }, indent=2)

    if resp.status_code >= 400:
        return json.dumps(
            _write_error_payload(resp, f"creative {full_urn} status update"),
            indent=2,
        )

    try:
        current = _get_raw(raw_url)
        new_status = current.get("intendedStatus") if isinstance(current, dict) else None
    except Exception as exc:
        return json.dumps({
            "status": "success",
            "warning": f"Write succeeded but re-read failed: {exc}",
            "account_id": account_id,
            "creative_urn": full_urn,
            "requested_intended_status": intended_status,
        }, indent=2, default=str)

    return json.dumps({
        "status": "success",
        "account_id": account_id,
        "creative_urn": full_urn,
        "requested_intended_status": intended_status,
        "current_intended_status": new_status,
        "confirmed": new_status == intended_status,
        "creative": current,
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


# ---------------------------------------------------------------------------
# Ad Library (public ad-transparency API) — READ ONLY
# Separate from the campaign-management/analytics endpoints above: this is the
# public ad library that lets you see the ads ANY company is publicly running
# (competitors or our own), for creative/messaging research.
#
#   Search:  GET /rest/adLibrary?q=criteria
#              &keyword=...&advertiser=...
#              &countries=List(US,GB)
#              &dateRange=(start:(year:Y,month:M,day:D),end:(...))
#              &start=0&count=25
#   Detail:  GET /rest/adLibrary/{adId}
#
# Uses the SAME LINKEDIN_ACCESS_TOKEN and versioned headers as every other read
# here. NOTE: the Ad Library API is a private Marketing API product that must be
# approved ON TOP OF r_ads/r_ads_reporting; an un-provisioned token gets a 404
# on /adLibrary. That case is surfaced as a clear, humanized error (below) that
# tells the operator to request Ad Library access and refresh the token — we do
# NOT hardcode or invent a separate credential.
#
# RestLi syntax (List(...), the dateRange tuple) is built into the raw URL and
# sent via httpx build_request+send so httpx's params= encoder does not double-
# encode it (same approach as the analytics/creatives readers above).
# ---------------------------------------------------------------------------


def _adlibrary_error_payload(resp: httpx.Response, action: str) -> dict:
    """Map a failed LinkedIn Ad Library read to a plain-language error dict."""
    status_code = resp.status_code
    raw_text = resp.text or ""
    li_message = ""
    li_code = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            li_message = str(body.get("message", "") or "")
            li_code = str(body.get("code", "") or body.get("serviceErrorCode", "") or "")
    except Exception:
        li_message = raw_text

    lower = f"{li_message} {li_code} {raw_text}".lower()
    li_text = li_message or raw_text

    if status_code == 404 or "not found" in lower or "no resource" in lower:
        error = "ad_library_not_enabled"
        message = (
            f"LinkedIn returned 404 for the Ad Library endpoint during {action}. "
            "The Ad Library API is a separate, PRIVATE Marketing API product that "
            "must be approved on top of the current r_ads / r_ads_reporting access. "
            "Apply for Ad Library API access in the LinkedIn Developer Portal "
            "(My Apps > your app > Products), and once approved, re-authenticate and "
            "update the LINKEDIN_ACCESS_TOKEN secret with a token carrying the new "
            f"product. LinkedIn said: {li_text}"
        )
    elif status_code in (401, 403) or "permission" in lower or "scope" in lower \
            or "access_denied" in lower or "not authorized" in lower:
        error = "permission_or_scope"
        message = (
            f"LinkedIn rejected the Ad Library {action} ({status_code}). The access "
            "token most likely lacks the Ad Library API product/scope. The Ad Library "
            "API requires a separate approval on top of r_ads; request it in the "
            "LinkedIn Developer Portal, then refresh the LINKEDIN_ACCESS_TOKEN secret. "
            f"LinkedIn said: {li_text}"
        )
    elif status_code == 426 or "nonexistent_version" in lower:
        error = "api_version_expired"
        message = (
            f"The LinkedIn API version ({LINKEDIN_API_VERSION}) is expired or no "
            "longer accepted. Bump LINKEDIN_API_VERSION in linkedin_mcp_server.py "
            f"to a currently-supported month. LinkedIn said: {li_text}"
        )
    elif status_code == 429 or ("rate" in lower and "limit" in lower):
        error = "rate_limited"
        message = (
            "LinkedIn rate-limited this Ad Library read (429). Wait a moment and "
            f"retry. LinkedIn said: {li_text}"
        )
    else:
        error = "linkedin_error"
        message = f"LinkedIn Ad Library API error during {action} ({status_code}): {li_text}"

    return {
        "status": "error",
        "error": error,
        "http_status": status_code,
        "linkedin_code": li_code,
        "message": message,
    }


def _adlibrary_fetch(raw_url: str, action: str) -> tuple:
    """
    GET a fully-built Ad Library URL via raw build_request+send (no param
    re-encoding). Returns (ok: bool, payload). On HTTP/transport failure,
    payload is a humanized error dict instead of raising.
    """
    try:
        with httpx.Client(timeout=30) as client:
            req = client.build_request("GET", raw_url, headers=_headers())
            resp = client.send(req)
    except Exception as exc:
        return False, {
            "status": "error",
            "error": "request_failed",
            "message": f"Request to LinkedIn Ad Library failed: {exc}",
        }
    if resp.status_code >= 400:
        return False, _adlibrary_error_payload(resp, action)
    try:
        return True, resp.json()
    except Exception:
        return True, {"raw_response": resp.text}


def _epoch_ms_to_date(value: Any) -> Optional[str]:
    """Convert a LinkedIn epoch-millisecond timestamp to a YYYY-MM-DD string."""
    try:
        return datetime.utcfromtimestamp(int(value) / 1000).strftime("%Y-%m-%d")
    except Exception:
        return None


def _ad_id_from_url(ad_url: str) -> Optional[str]:
    """Pull the numeric ad id out of an Ad Library detail URL."""
    if not ad_url:
        return None
    return ad_url.rstrip("/").split("/")[-1].split("?")[0] or None


def _is_ad_library_url(url: str) -> bool:
    """
    True only for a public LinkedIn Ad Library detail URL
    (https://www.linkedin.com/ad-library/detail/{id}). Used to block SSRF: the
    scraper must never be pointed at an arbitrary/internal host.
    """
    try:
        parsed = urlparse(url or "")
    except Exception:
        return False
    return (
        parsed.scheme == "https"
        and parsed.netloc.lower() in ("www.linkedin.com", "linkedin.com")
        and parsed.path.startswith("/ad-library/detail/")
    )


def _simplify_ad(ad: Any) -> dict:
    """
    Project one Ad Library search element into comparison-friendly METADATA.

    The Ad Library `q=criteria` finder returns metadata only — advertiser,
    ad type, impression range, active dates, targeting, and the public detail
    URL. It does NOT return the creative copy (headline / body / CTA); that lives
    on the public detail page and is fetched by linkedin_get_ad_library_ad (or by
    passing include_creative=True to the search). The full untouched record is
    kept under `raw`.
    """
    if not isinstance(ad, dict):
        return {"raw": ad}

    details = ad.get("details", {}) if isinstance(ad.get("details"), dict) else {}
    advertiser = details.get("advertiser", {}) if isinstance(details.get("advertiser"), dict) else {}
    stats = details.get("adStatistics", {}) if isinstance(details.get("adStatistics"), dict) else {}
    targeting = details.get("adTargeting", []) if isinstance(details.get("adTargeting"), list) else []

    countries = None
    for facet in targeting:
        if isinstance(facet, dict) and facet.get("facetName") == "Location":
            countries = facet.get("includedSegments") or None
            break

    impressions = stats.get("totalImpressions")
    if isinstance(impressions, dict):
        impressions = {"from": impressions.get("from"), "to": impressions.get("to")}

    ad_url = ad.get("adUrl")
    return {
        "ad_id": _ad_id_from_url(ad_url),
        "ad_url": ad_url,
        "advertiser": advertiser.get("advertiserName"),
        "advertiser_legal_name": advertiser.get("adPayer"),
        "advertiser_url": advertiser.get("advertiserUrl"),
        "format": details.get("type"),
        "total_impressions_range": impressions,
        "first_shown": _epoch_ms_to_date(stats.get("firstImpressionAt")),
        "last_shown": _epoch_ms_to_date(stats.get("latestImpressionAt")),
        "countries": countries,
        "targeting": targeting,
        "is_restricted": ad.get("isRestricted"),
        "note": (
            "Creative copy (headline/body/CTA) is not in the search response — "
            "fetch it with linkedin_get_ad_library_ad(ad_id) or pass "
            "include_creative=True."
        ),
        "raw": ad,
    }


def _fetch_ad_creative(ad_url: str) -> dict:
    """
    Fetch the PUBLIC Ad Library detail page (no auth required) and extract the
    actual creative copy LinkedIn does not expose via the API: advertiser name,
    headline, body copy, and the call-to-action button label.

    The landing/destination URL is intentionally NOT shown by LinkedIn's public
    Ad Library (the CTA button carries no href), so it cannot be returned.

    Returns a dict with the extracted fields, or {"error": ...} on failure.
    """
    if not ad_url:
        return {"error": "no_ad_url", "message": "No public ad URL to fetch."}
    if not _is_ad_library_url(ad_url):
        return {
            "error": "invalid_ad_url",
            "message": (
                "Refusing to fetch a non Ad Library URL; only "
                "https://www.linkedin.com/ad-library/detail/ pages are allowed."
            ),
        }
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        return {"error": "parser_unavailable", "message": f"BeautifulSoup not available: {exc}"}

    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            resp = client.get(ad_url, headers={"User-Agent": "Mozilla/5.0"})
    except Exception as exc:
        return {"error": "request_failed", "message": f"Fetch of {ad_url} failed: {exc}"}

    if resp.status_code == 404:
        return {"error": "ad_not_found", "message": f"Ad detail page not found (404): {ad_url}"}
    if resp.status_code >= 400:
        return {"error": "page_error", "message": f"Ad detail page error {resp.status_code}: {ad_url}"}

    soup = BeautifulSoup(resp.text, "html.parser")

    body_el = soup.select_one(".commentary__content")
    body = body_el.get_text("\n", strip=True) if body_el else None

    headline = None
    cta = None
    head_el = soup.select_one(".sponsored-content-headline")
    if head_el:
        cta_btn = head_el.find(
            attrs={"data-tracking-control-name": "ad_library_ad_detail_cta"}
        )
        if cta_btn:
            cta = cta_btn.get_text(strip=True) or None
            cta_btn.extract()
        headline = head_el.get_text(" ", strip=True) or None

    advertiser = None
    adv_el = soup.find(
        "a", attrs={"data-tracking-control-name": "ad_library_ad_preview_advertiser"}
    )
    if adv_el:
        advertiser = adv_el.get_text(strip=True) or None

    return {
        "advertiser": advertiser,
        "headline": headline,
        "body": body,
        "call_to_action": cta,
        "landing_url": None,
        "landing_url_note": (
            "LinkedIn's public Ad Library does not expose the destination/landing "
            "URL for an ad, so it is not available."
        ),
        "ad_url": ad_url,
    }


@mcp.tool()
def linkedin_search_ad_library(
    advertiser: Optional[str] = None,
    keyword: Optional[str] = None,
    countries: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    count: int = 25,
    max_results: int = 50,
    include_creative: bool = False,
) -> str:
    """
    Search the public LinkedIn Ad Library for ads a company is currently running.

    This is the ad-TRANSPARENCY API (separate from the campaign-analytics tools):
    it returns the ads ANY company is publicly running — a competitor's OR our own
    — so the agent can read and compare creative/messaging. Use this (not the
    campaign tools) when the user names a company and wants to see "what ads is X
    running" or "us vs. competitor, how do we improve". The same single tool
    covers both sides: search the competitor's name for theirs, search our company
    name for ours, then reason over the two result sets.

    At least one of `advertiser` or `keyword` is required. The Ad Library is a
    LIVE snapshot of ACTIVE ads (no historical archive), so an ad that stopped
    running will not appear.

    The search API returns METADATA per ad (advertiser, ad format/type,
    impression range, active dates, targeting incl. countries, and the public
    detail URL). The creative COPY (headline / body / call-to-action) is not in
    the search response — set include_creative=True to also fetch each ad's copy
    from its public detail page, or call linkedin_get_ad_library_ad per ad.

    Args:
        advertiser: Advertiser / company name to filter by (e.g. "Salesforce").
        keyword: Free-text keyword matched against ad copy and advertiser name
            (e.g. "marketing automation"). Combine with or use instead of
            `advertiser`.
        countries: Optional comma-separated ISO 3166-1 alpha-2 country codes to
            narrow where the ads ran (e.g. "US,GB,DE").
        start_date: Optional date-range start, YYYY-MM-DD (requires end_date).
        end_date: Optional date-range end, YYYY-MM-DD (requires start_date).
        count: Page size per request (default 25, max 100).
        max_results: Cap on total ads returned across pages (default 50). Raise
            for large advertisers; pagination follows LinkedIn's `paging.total`.
        include_creative: When True, also fetch each returned ad's headline, body
            copy, and CTA from its public detail page. This adds one HTTP request
            per ad, so keep max_results modest (e.g. <= 15) when enabling it.

    Returns:
        JSON with the matched ads. Each ad carries comparison-friendly metadata
        (advertiser, format, active date range, impressions range, targeting,
        countries, public ad_url) plus the full `raw` record; with
        include_creative=True each ad also gets a `creative` block (headline,
        body, call_to_action). On a missing Ad Library product/scope, returns a
        clear, humanized error explaining how to request access.
    """
    if not (advertiser or keyword):
        return json.dumps({
            "status": "error",
            "error": "invalid_request",
            "message": (
                "Provide at least one of `advertiser` (company name) or `keyword` "
                "to search the Ad Library."
            ),
        }, indent=2)

    if (start_date and not end_date) or (end_date and not start_date):
        return json.dumps({
            "status": "error",
            "error": "invalid_request",
            "message": "start_date and end_date must be provided together (YYYY-MM-DD).",
        }, indent=2)

    page_size = max(1, min(int(count or 25), 100))
    max_results = max(1, int(max_results or 50))

    parts = ["q=criteria"]
    if keyword:
        parts.append(f"keyword={quote(keyword, safe='')}")
    if advertiser:
        parts.append(f"advertiser={quote(advertiser, safe='')}")
    if countries:
        codes = [c.strip().upper() for c in countries.split(",") if c.strip()]
        if codes:
            parts.append(f"countries=List({','.join(codes)})")
    if start_date and end_date:
        parts.append(f"dateRange={_build_date_range(start_date, end_date)}")
    base = "&".join(parts)

    all_ads: list = []
    start = 0
    while True:
        raw_url = (
            f"{LINKEDIN_REST_BASE}/adLibrary?{base}"
            f"&start={start}&count={page_size}"
        )
        ok, payload = _adlibrary_fetch(raw_url, "search")
        if not ok:
            return json.dumps(payload, indent=2)
        elements = payload.get("elements", []) if isinstance(payload, dict) else []
        all_ads.extend(elements)
        paging = payload.get("paging", {}) if isinstance(payload, dict) else {}
        total = paging.get("total", len(all_ads))
        fetched = start + len(elements)
        if (
            len(elements) < page_size
            or fetched >= total
            or len(all_ads) >= max_results
        ):
            break
        start += page_size

    all_ads = all_ads[:max_results]
    ads = [_simplify_ad(a) for a in all_ads]

    if include_creative:
        for ad in ads:
            ad["creative"] = _fetch_ad_creative(ad.get("ad_url"))
            ad.pop("note", None)

    return json.dumps({
        "status": "success",
        "filters": {
            "advertiser": advertiser,
            "keyword": keyword,
            "countries": countries,
            "start_date": start_date,
            "end_date": end_date,
        },
        "total_ads": len(ads),
        "note": (
            "The Ad Library is a live snapshot of ACTIVE ads only (no historical "
            "archive). Search returns metadata; creative copy (headline/body/CTA) "
            "comes from include_creative=True or linkedin_get_ad_library_ad. The "
            "destination/landing URL is not exposed by LinkedIn's public Ad "
            "Library. For 'us vs. competitor' requests, run this once per company "
            "and compare the result sets."
        ),
        "ads": ads,
    }, indent=2, default=str)


@mcp.tool()
def linkedin_get_ad_library_ad(ad_id: str) -> str:
    """
    Fetch the full creative detail for a single LinkedIn Ad Library ad.

    Use after linkedin_search_ad_library when the agent needs the actual ad COPY
    (headline, body, call-to-action) that the search metadata does not include.
    The detail comes from the ad's PUBLIC Ad Library page (the `ad_url` /
    `ad_id` from a search result) — the Ad Library API itself has no per-ad GET
    endpoint, so this reads the public detail page (no extra credential needed).

    Args:
        ad_id: The Ad Library ad id from a linkedin_search_ad_library result
            (the numeric id, or the full public ad_url — both are accepted).

    Returns:
        JSON with the ad's advertiser, headline, body copy, and call-to-action.
        The destination/landing URL is NOT exposed by LinkedIn's public Ad
        Library and is therefore returned as null. Returns a clear error if the
        ad page cannot be fetched.
    """
    raw = (ad_id or "").strip()
    if not raw:
        return json.dumps({
            "status": "error",
            "error": "invalid_request",
            "message": "ad_id is required (a numeric Ad Library id or its public ad_url).",
        }, indent=2)

    if raw.startswith("http"):
        resolved_id = _ad_id_from_url(raw)
        if not _is_ad_library_url(raw) or not (resolved_id and resolved_id.isdigit()):
            return json.dumps({
                "status": "error",
                "error": "invalid_request",
                "message": (
                    "ad_id must be a numeric Ad Library id, or a public LinkedIn "
                    "Ad Library detail URL of the form "
                    "https://www.linkedin.com/ad-library/detail/{id}."
                ),
            }, indent=2)
        ad_url = f"https://www.linkedin.com/ad-library/detail/{resolved_id}"
    else:
        if not raw.isdigit():
            return json.dumps({
                "status": "error",
                "error": "invalid_request",
                "message": "ad_id must be the numeric Ad Library id (or its public ad_url).",
            }, indent=2)
        resolved_id = raw
        ad_url = f"https://www.linkedin.com/ad-library/detail/{raw}"

    creative = _fetch_ad_creative(ad_url)
    if creative.get("error"):
        return json.dumps({
            "status": "error",
            "error": creative["error"],
            "message": creative.get("message", "Failed to fetch the ad detail page."),
            "ad_id": resolved_id,
            "ad_url": ad_url,
        }, indent=2)

    return json.dumps({
        "status": "success",
        "ad_id": resolved_id,
        "ad": creative,
    }, indent=2, default=str)


if __name__ == "__main__":
    mcp.run(transport="stdio")
