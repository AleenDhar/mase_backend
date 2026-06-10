import os
import json
import base64
import time
import threading
import logging
from datetime import datetime
from datetime import timezone as _tz_utc_module
from typing import Optional, List, Any

import httpx
from fastmcp import FastMCP
from simple_salesforce import Salesforce

_sf_logger = logging.getLogger("lemlist_sf_sync")
_sf_logger.setLevel(logging.INFO)

BASE_URL = "https://api.lemlist.com/api"
API_KEY = os.environ.get("LEMLIST_API_KEY", "")


def _clean_first_name(name: str, last_name: str = "") -> str:
    """Strip a spurious trailing last-name initial fused onto the first name.

    When last_name is given: strip trailing uppercase only if it matches last_name[0].
    When last_name is absent: fall back to stripping any trailing [lower][UPPER] pattern.
    Single-letter names, all-lowercase endings, and non-matching trailing letters are
    always returned unchanged.
    """
    if not name or len(name) <= 1:
        return name
    trailing = name[-1]
    prev = name[-2]
    if not (trailing.isupper() and prev.islower()):
        return name
    last_name = str(last_name) if last_name is not None else ""
    if last_name:
        if trailing == last_name[0].upper():
            return name[:-1]
        return name
    return name[:-1]

_api_lock = threading.Lock()
_last_request_time = 0.0
_MIN_REQUEST_INTERVAL = 0.15

mcp = FastMCP(
    name="Lemlist",
    instructions=(
        "Use this server to interact with the Lemlist outreach and sales engagement platform. "
        "You can manage campaigns, leads, activities, unsubscribes, enrichment, schedules, "
        "team members, webhooks, tasks, and more. All operations require a valid Lemlist API key "
        "set via the LEMLIST_API_KEY environment variable. "
        "Lemlist uses Basic auth with empty username and API key as password. "
        "IMPORTANT — Tool selection for call-related questions: "
        "(1) PENDING call tasks per campaign (how many calls are left to make): "
        "use lemlist_get_pending_call_tasks with a comma-separated list of campaign IDs. "
        "NEVER use lemlist_get_tasks for this — it does not support campaignId or status filters "
        "and will always return a 400 Malformed filters error. "
        "(2) HISTORICAL calls made in a date range (call counts per day, connected vs not): "
        "use lemlist_get_calls_report with campaign IDs and start/end dates. "
        "Do NOT use lemlist_get_activities for call questions — it has no date filter and times out. "
        "(3) EMAIL ACTIVITY AGGREGATED BY BDR for a date range (how many emails sent / opened / "
        "clicked / replied / bounced per sender): use lemlist_activity_summary_by_user. It does "
        "all pagination + filtering server-side and returns a compact per-BDR table — do NOT use "
        "lemlist_get_activities for this, it will exhaust the context window. "
        "(4) PUSHING CONTACTS TO A CAMPAIGN: "
        "  - ABM / batch pushes from Salesforce: use lemlist_validated_push. It "
        "    re-queries SF for each contact, rejects cross-account contacts, "
        "    resolves owner_email to a Lemlist sender, fail-closes on conflicts, "
        "    and writes one row per attempt to public.lemlist_push_receipts "
        "    (with api_endpoint = the exact Lemlist URL that was called). Use "
        "    lemlist_get_push_receipts(chat_id) to verify what actually went out. "
        "  - One-off / ad-hoc pushes where you don't need SF re-validation: use "
        "    lemlist_add_lead_to_campaign. Pass the full Salesforce-derived "
        "    identity fields (email, first_name, last_name, job_title, phone, "
        "    linkedin_url, company_name, contact_owner) and put personalization "
        "    variables (customSubject1, customBody1, customBridge1, customValue1, "
        "    CTA1, linkedInMessage, etc.) in the custom_fields dict. If Lemlist "
        "    rejects the push the tool returns the error in its response — read "
        "    it and act on it."
    ),
)


def _headers() -> dict:
    if not API_KEY:
        raise RuntimeError(
            "LEMLIST_API_KEY environment variable is not set. "
            "Please set it to your Lemlist API key."
        )
    encoded = base64.b64encode(f":{API_KEY}".encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _throttle():
    global _last_request_time
    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
    _last_request_time = time.monotonic()


def _request_with_retry(method: str, url: str, headers: dict,
                        json_payload=None, params=None,
                        max_retries: int = 4) -> httpx.Response:
    for attempt in range(max_retries):
        with _api_lock:
            _throttle()
            with httpx.Client(timeout=30) as client:
                if method == "GET":
                    resp = client.get(url, headers=headers, params=params)
                elif method == "POST":
                    resp = client.post(url, headers=headers, json=json_payload if json_payload is not None else {}, params=params)
                elif method == "PATCH":
                    resp = client.patch(url, headers=headers, json=json_payload)
                elif method == "DELETE":
                    resp = client.delete(url, headers=headers)
                else:
                    raise ValueError(f"Unsupported method: {method}")

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = float(retry_after)
                except ValueError:
                    wait = 2.0 * (attempt + 1)
            else:
                wait = 2.0 * (attempt + 1)
            if attempt < max_retries - 1:
                time.sleep(wait)
                continue
        return resp
    return resp


def _raise_with_detail(resp):
    if resp.status_code >= 400:
        try:
            body = resp.text
        except Exception:
            body = ""
        raise RuntimeError(
            f"Lemlist API error {resp.status_code} for {resp.request.method} {resp.url}: {body}"
        )


def _get(path: str, params: Optional[dict] = None) -> Any:
    url = f"{BASE_URL}{path}"
    resp = _request_with_retry("GET", url, _headers(), params=params)
    _raise_with_detail(resp)
    if not resp.text or not resp.text.strip():
        return None
    content_type = resp.headers.get("content-type", "")
    if "application/json" not in content_type and "text/json" not in content_type:
        if resp.text.strip().startswith(("<", "<!DOCTYPE")):
            raise RuntimeError(
                f"Lemlist API returned HTML instead of JSON for GET {url}. "
                f"This endpoint may not exist or may require a different API path."
            )
    return resp.json()


def _post(path: str, payload=None) -> Any:
    url = f"{BASE_URL}{path}"
    resp = _request_with_retry("POST", url, _headers(), json_payload=payload)
    _raise_with_detail(resp)
    try:
        return resp.json()
    except Exception:
        return {"status": "ok", "status_code": resp.status_code}


def _patch(path: str, payload: dict) -> Any:
    url = f"{BASE_URL}{path}"
    resp = _request_with_retry("PATCH", url, _headers(), json_payload=payload)
    _raise_with_detail(resp)
    try:
        return resp.json()
    except Exception:
        return {"status": "ok", "status_code": resp.status_code}


def _delete(path: str) -> Any:
    url = f"{BASE_URL}{path}"
    resp = _request_with_retry("DELETE", url, _headers())
    _raise_with_detail(resp)
    try:
        return resp.json()
    except Exception:
        return {"status": "ok", "status_code": resp.status_code}


@mcp.tool()
def lemlist_get_team() -> str:
    """
    Get information about the current team/workspace.

    Returns:
        JSON string with team details.
    """
    result = _get("/team")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_get_users() -> str:
    """
    List all users (senders) in the current team, including their associated campaigns
    and sending channels.

    Returns:
        JSON string with array of user/sender objects, each containing userId
        and their campaigns with status and sending channels.
    """
    result = _get("/team/senders")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_get_user_channels(user_id: str) -> str:
    """
    Get the channels (email accounts, LinkedIn, etc.) configured for a specific user.

    Args:
        user_id: The user's ID.

    Returns:
        JSON string with the user's configured channels.
    """
    result = _get(f"/users/{user_id}/channels")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_list_campaigns(
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    status: Optional[str] = None,
) -> str:
    """
    List all campaigns in the workspace.

    Args:
        offset: Number of campaigns to skip (for pagination).
        limit: Maximum number of campaigns to return.
        status: Filter by status: "draft", "running", "paused", "stopped".

    Returns:
        JSON string with array of campaign objects.
    """
    params = {}
    if offset is not None:
        params["offset"] = offset
    if limit is not None:
        params["limit"] = limit
    if status:
        params["status"] = status
    result = _get("/campaigns", params=params if params else None)
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_get_campaign(campaign_id: str) -> str:
    """
    Get details of a specific campaign by ID.

    Args:
        campaign_id: The campaign's ID.

    Returns:
        JSON string with campaign details including name, status, steps, etc.
    """
    result = _get(f"/campaigns/{campaign_id}")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_get_campaign_stats(campaign_id: str) -> str:
    """
    Get statistics for a specific campaign (sent, opened, clicked, replied, etc.).

    Args:
        campaign_id: The campaign's ID.

    Returns:
        JSON string with campaign statistics.
    """
    result = _get(f"/campaigns/{campaign_id}/stats")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_export_campaign(campaign_id: str) -> str:
    """
    Export all leads from a campaign with their current status and activity data.

    Args:
        campaign_id: The campaign's ID.

    Returns:
        JSON string with exported campaign lead data.
    """
    result = _get(f"/campaigns/{campaign_id}/export")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_pause_campaign(campaign_id: str) -> str:
    """
    Pause a running campaign. Use this before adding leads to avoid 500 errors,
    then call lemlist_start_campaign to resume after leads are added.

    Args:
        campaign_id: The campaign's ID.

    Returns:
        JSON string confirming the campaign was paused.
    """
    result = _post(f"/campaigns/{campaign_id}/pause")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_start_campaign(campaign_id: str) -> str:
    """
    Start or resume a paused campaign. If the campaign is already running,
    this does nothing. Call this after adding leads to a paused campaign.

    Args:
        campaign_id: The campaign's ID.

    Returns:
        JSON string confirming the campaign is running.
    """
    result = _post(f"/campaigns/{campaign_id}/start")
    return json.dumps(result, indent=2)


def _get_sf_connection():
    sf_user = os.environ.get("SF_USERNAME", "")
    sf_pass = os.environ.get("SF_PASSWORD", "")
    sf_token = os.environ.get("SF_SECURITY_TOKEN", "")
    sf_domain = os.environ.get("SF_DOMAIN", "login")
    if not sf_user or not sf_pass or not sf_token:
        return None
    try:
        return Salesforce(
            username=sf_user,
            password=sf_pass,
            security_token=sf_token,
            domain=sf_domain,
        )
    except Exception as e:
        _sf_logger.error(f"Salesforce connection failed: {e}")
        return None


def _sync_campaign_date_to_salesforce(email: str, added_date: str = None):
    sf = _get_sf_connection()
    if sf is None:
        _sf_logger.warning(f"Skipping SF sync for {email}: no Salesforce connection")
        return
    if not added_date:
        added_date = datetime.now(_tz_utc_module.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
    safe_email = email.replace("\\", "\\\\").replace("'", "\\'")
    try:
        results = sf.query(f"SELECT Id FROM Contact WHERE Email = '{safe_email}' LIMIT 1")
        records = results.get("records", [])
        if records:
            contact_id = records[0]["Id"]
            sf.Contact.update(contact_id, {
                "Lemlist_Campaign_Added_Date__c": added_date,
            })
            _sf_logger.info(f"SF synced Lemlist_Campaign_Added_Date__c for Contact {contact_id} ({email})")
        else:
            _sf_logger.warning(f"No Salesforce Contact found for email: {email}")
    except Exception as e:
        _sf_logger.error(f"SF sync failed for {email}: {e}")


@mcp.tool()
def lemlist_add_lead_to_campaign(
    campaign_id: str,
    email: str,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    company_name: Optional[str] = None,
    job_title: Optional[str] = None,
    phone: Optional[str] = None,
    linkedin_url: Optional[str] = None,
    picture_url: Optional[str] = None,
    company_domain: Optional[str] = None,
    ice_breaker: Optional[str] = None,
    timezone: Optional[str] = None,
    contact_owner: Optional[str] = None,
    custom_fields: Optional[dict] = None,
    deduplicate: bool = True,
    linkedin_enrichment: bool = False,
    verify_email: bool = False,
) -> str:
    """
    Add a lead to a specific campaign with full personalization support.

    Standard fields (first_name, last_name, email, company_name, job_title,
    phone, linkedin_url, contact_owner) map to Lemlist's built-in lead
    properties. Any extra personalization variables (customSubject1,
    customBody1, customBridge1, customValue1, CTA1, linkedInMessage, etc.)
    should be passed via custom_fields — these become template variables
    used in the campaign's email sequences.

    If Lemlist rejects the push (lead already in the campaign, invalid email,
    auth error, etc.) the tool returns the API error in its response so you
    can read it and decide what to do next.

    Args:
        campaign_id: The campaign's ID (e.g. "cam_abc123").
        email: Lead's email address (required).
        first_name: Lead's first name.
        last_name: Lead's last name.
        company_name: Lead's company name.
        job_title: Lead's job title.
        phone: Lead's phone number.
        linkedin_url: Lead's LinkedIn profile URL.
        picture_url: URL to lead's profile picture.
        company_domain: Lead's company domain (e.g. "acme.com").
        ice_breaker: Personalized ice breaker text for the email.
        timezone: Lead's timezone in IANA format (e.g. "Europe/Paris", "America/New_York").
        contact_owner: Contact owner (user ID or user login email).
        custom_fields: Dict of custom variable key/value pairs for email personalization.
            Common keys for multi-step outreach sequences:
            - customSubject1, customSubject2, customSubject3 (email subjects)
            - customBody1, customBody2, customBody3 (email body text)
            - customBridge1, customBridge2, customBridge3 (call-to-action text)
            - customValue1, customValue2, customValue3 (value propositions)
            - linkedInMessage (LinkedIn outreach message)
            Any key here becomes a {{variable}} in Lemlist email templates.
        deduplicate: If True (default), skip if lead already exists in any campaign.
        linkedin_enrichment: If True, run LinkedIn enrichment on the lead. Default False.
        verify_email: If True, verify the email address (debounce). Default False.

    Returns:
        JSON string with the created lead object including campaignId, _id, contactId.
    """
    payload: dict = {"email": email}
    if first_name:
        payload["firstName"] = _clean_first_name(first_name, last_name or "")
    if last_name:
        payload["lastName"] = last_name
    if company_name:
        payload["companyName"] = company_name
    if job_title:
        payload["jobTitle"] = job_title
    if phone:
        payload["phone"] = phone
    if linkedin_url:
        payload["linkedinUrl"] = linkedin_url
    if picture_url:
        payload["picture"] = picture_url
    if company_domain:
        payload["companyDomain"] = company_domain
    if ice_breaker:
        payload["icebreaker"] = ice_breaker
    if timezone:
        payload["timezone"] = timezone
    if contact_owner:
        payload["contactOwner"] = contact_owner
    if custom_fields:
        # Zone 1 identity fields (firstName, lastName, email, contactOwner, etc.) are
        # set exclusively via the root-level parameters above. Allowing custom_fields to
        # map them would let a misplaced value silently overwrite the correct one.
        # firstName/first_name and lastName/last_name are intentionally excluded here.
        STANDARD_FIELD_MAP = {
            "contactOwner": "contactOwner",
            "contact_owner": "contactOwner",
            "jobTitle": "jobTitle",
            "job_title": "jobTitle",
            "companyName": "companyName",
            "company_name": "companyName",
            "companyDomain": "companyDomain",
            "company_domain": "companyDomain",
            "linkedinUrl": "linkedinUrl",
            "linkedin_url": "linkedinUrl",
        }
        ZONE1_IDENTITY_KEYS = {
            "firstName", "first_name", "lastName", "last_name", "email",
        }
        for k, v in custom_fields.items():
            if k in ZONE1_IDENTITY_KEYS:
                continue
            if v is None or (isinstance(v, str) and v.strip() == ""):
                continue
            mapped_key = STANDARD_FIELD_MAP.get(k, k)
            payload[mapped_key] = v

    query_params = []
    if deduplicate:
        query_params.append("deduplicate=true")
    if linkedin_enrichment:
        query_params.append("linkedinEnrichment=true")
    if verify_email:
        query_params.append("verifyEmail=true")
    params_str = "?" + "&".join(query_params) if query_params else ""

    url_path = f"/campaigns/{campaign_id}/leads{params_str}"
    added_date = datetime.now(_tz_utc_module.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")

    try:
        result = _post(url_path, payload)
        _sync_campaign_date_to_salesforce(email, added_date)
        return json.dumps(result, indent=2)
    except RuntimeError as e:
        if "500" not in str(e):
            raise

        was_running = False
        try:
            camp_info = _get(f"/campaigns/{campaign_id}")
            was_running = camp_info.get("state") == "running" or camp_info.get("status") == "running"
        except Exception:
            pass

        if was_running:
            try:
                _post(f"/campaigns/{campaign_id}/pause")
            except Exception:
                pass
            time.sleep(2)

        for retry in range(3):
            try:
                result = _post(url_path, payload)
                if was_running:
                    try:
                        _post(f"/campaigns/{campaign_id}/start")
                    except Exception:
                        pass
                _sync_campaign_date_to_salesforce(email, added_date)
                return json.dumps(result, indent=2)
            except RuntimeError as retry_err:
                if "500" in str(retry_err) and retry < 2:
                    time.sleep(3 * (retry + 1))
                    continue
                if was_running:
                    try:
                        _post(f"/campaigns/{campaign_id}/start")
                    except Exception:
                        pass
                raise RuntimeError(
                    f"Lemlist 500 error persists after pausing campaign and {retry + 1} retries. "
                    f"Campaign ID: {campaign_id}. "
                    f"Try: 1) Duplicate the campaign in Lemlist UI and use the new campaign ID, "
                    f"or 2) Contact Lemlist support about campaign {campaign_id}. "
                    f"Original error: {retry_err}"
                )


@mcp.tool()
def lemlist_get_lead(email: str) -> str:
    """
    Get a lead by their email address across all campaigns.

    Args:
        email: The lead's email address.

    Returns:
        JSON string with the lead's details and campaign membership.
    """
    result = _get(f"/leads/{email}")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_get_lead_in_campaign(campaign_id: str, email: str) -> str:
    """
    Get a lead's details (including _id) from ONE KNOWN campaign.

    USE THIS TOOL ONLY when you already know the campaign_id and need the
    lead's _id to call lemlist_update_lead. It is a targeted lookup, not a
    discovery tool.

    ⚠️ DO NOT call this in a loop across campaigns to find where a lead is
    enrolled — that makes N redundant API calls and floods the context window.
    To discover which campaigns a lead belongs to, use lemlist_get_lead(email)
    instead — ONE call returns all campaign memberships across the account.

    Args:
        campaign_id: The campaign's ID (must be known in advance).
        email: The lead's email address.

    Returns:
        JSON string with the lead's details including _id (lead ID),
        firstName, lastName, email, linkedinUrl, phone, companyName,
        contactOwner, custom variables, and campaign membership.
    """
    result = _get(f"/leads/{email}")
    if not result or not isinstance(result, dict):
        return json.dumps({
            "error": "Lead not found. The email may not exist in any campaign. Verify the email address and try again.",
            "campaign_id": campaign_id,
            "email": email
        }, indent=2)
    found_campaign = result.get("campaignId", "")
    if found_campaign and found_campaign != campaign_id:
        result["_warning"] = (
            f"Lead found in campaign {found_campaign}, not in the requested campaign {campaign_id}. "
            f"Use the correct campaign_id ({found_campaign}) when calling lemlist_update_lead."
        )
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_update_lead(
    campaign_id: str,
    lead_id: str,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    company_name: Optional[str] = None,
    job_title: Optional[str] = None,
    phone: Optional[str] = None,
    linkedin_url: Optional[str] = None,
    contact_owner: Optional[str] = None,
    custom_fields: Optional[dict] = None,
) -> str:
    """
    Update an existing lead in a campaign. Only the fields you provide will
    be updated — all other fields remain unchanged.

    To get the lead_id, first call lemlist_get_lead_in_campaign with the
    campaign_id and email, then use the _id field from the response.

    Args:
        campaign_id: The campaign's ID.
        lead_id: The lead's unique ID (e.g. lea_XXXX). Get this from
            lemlist_get_lead_in_campaign response _id field.
        first_name: Updated first name (optional).
        last_name: Updated last name (optional).
        company_name: Updated company name (optional).
        job_title: Updated job title (optional).
        phone: Updated phone number (optional).
        linkedin_url: Updated LinkedIn profile URL (optional).
        contact_owner: Updated contact owner email (optional).
        custom_fields: Dict of custom variables to update (optional).
            Example: {"customBody1": "new body", "linkedInMessage": "new msg"}
            Only the keys you include will be updated.

    Returns:
        JSON string with the updated lead details.
    """
    payload = {}
    if first_name is not None:
        payload["firstName"] = first_name
    if last_name is not None:
        payload["lastName"] = last_name
    if company_name is not None:
        payload["companyName"] = company_name
    if job_title is not None:
        payload["jobTitle"] = job_title
    if phone is not None:
        payload["phone"] = phone
    if linkedin_url is not None:
        payload["linkedinUrl"] = linkedin_url
    if contact_owner is not None:
        payload["contactOwner"] = contact_owner
    if custom_fields:
        for key, value in custom_fields.items():
            if value is not None and value != "":
                payload[key] = value

    if not payload:
        raise RuntimeError("No fields provided to update. Provide at least one non-empty field.")

    result = _patch(f"/campaigns/{campaign_id}/leads/{lead_id}", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_delete_lead_from_campaign(campaign_id: str, email: str) -> str:
    """
    Remove a lead from a specific campaign.

    Args:
        campaign_id: The campaign's ID.
        email: The lead's email address.

    Returns:
        JSON string confirming deletion.
    """
    result = _delete(f"/campaigns/{campaign_id}/leads/{email}")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_mark_lead_interested(campaign_id: str, email: str) -> str:
    """
    Mark a lead as interested in a campaign.

    Args:
        campaign_id: The campaign's ID.
        email: The lead's email address.

    Returns:
        JSON string confirming the action.
    """
    result = _post(f"/campaigns/{campaign_id}/leads/{email}/interested")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_mark_lead_not_interested(campaign_id: str, email: str) -> str:
    """
    Mark a lead as not interested in a campaign.

    Args:
        campaign_id: The campaign's ID.
        email: The lead's email address.

    Returns:
        JSON string confirming the action.
    """
    result = _post(f"/campaigns/{campaign_id}/leads/{email}/notInterested")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_get_activities(
    campaign_id: Optional[str] = None,
    activity_type: Optional[str] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
) -> str:
    """
    Get campaign activities (emails sent, opened, clicked, replied, etc.).

    Args:
        campaign_id: Filter by campaign ID. If omitted, returns activities across all campaigns.
        activity_type: Filter by type. Options include:
            Email: "emailsSent", "emailsOpened", "emailsClicked", "emailsReplied", "emailsBounced"
            LinkedIn: "linkedinInviteSent", "linkedinMessageSent", "linkedinReplied", "linkedinAccepted"
            Other: "interested", "notInterested", "unsubscribed", "taskCompleted", "meetingBooked",
                   "paused", "resumed", "failed", "aircallCreated", "whatsappSent"
        offset: Number of activities to skip.
        limit: Maximum number of activities to return.

    Returns:
        JSON string with array of activity objects.
    """
    params = {}
    if campaign_id:
        params["campaignId"] = campaign_id
    if activity_type:
        params["type"] = activity_type
    if offset is not None:
        params["offset"] = offset
    if limit is not None:
        params["limit"] = limit
    result = _get("/activities", params=params if params else None)
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_get_calls_report(
    campaign_ids: str,
    start_date: str,
    end_date: str,
) -> str:
    """
    Get a complete calls report across one or more Lemlist campaigns for a date range.

    This is the PRIMARY tool to use when asked about calls made in Lemlist campaigns.
    It handles pagination internally, filters by date, and returns both a per-day
    summary table and a full per-campaign breakdown.

    NOTE: Lemlist tracks "calls initiated" via the aircallCreated activity type.
    Whether a call was "connected/answered" is stored as a status field on each
    activity (connected, voicemail, no_answer, busy, failed). This tool groups
    by status so you can see calls connected vs not connected.

    Args:
        campaign_ids: Comma-separated list of campaign IDs, e.g.
                      "cam_ABC123,cam_DEF456,cam_GHI789"
        start_date:   Start date inclusive, format YYYY-MM-DD (e.g. "2026-04-01")
        end_date:     End date inclusive, format YYYY-MM-DD (e.g. "2026-04-17")

    Returns:
        JSON with:
          - summary_table: list of {date, total_calls, connected, voicemail, no_answer, other}
          - per_campaign: breakdown by campaign
          - all_calls: full list of individual call activity records in date range
          - note: any caveats about data availability
    """
    from collections import defaultdict

    ids = [c.strip() for c in campaign_ids.split(",") if c.strip()]
    if not ids:
        return json.dumps({"error": "No campaign IDs provided."}, indent=2)

    try:
        start_dt = _parse_date_param(start_date, end_of_day=False)
        end_dt = _parse_date_param(end_date, end_of_day=True)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)

    all_calls: list = []
    per_campaign_counts: dict = {}
    errors: list = []

    for cam_id in ids:
        cam_calls: list = []
        offset = 0
        page_size = 100
        while True:
            params = {
                "campaignId": cam_id,
                "type": "aircallCreated",
                "offset": offset,
                "limit": page_size,
            }
            try:
                raw = _get("/activities", params=params)
            except Exception as exc:
                errors.append(f"{cam_id}: {exc}")
                break

            # Lemlist returns either a list or {"data": [...]}
            if isinstance(raw, list):
                items = raw
            elif isinstance(raw, dict):
                items = raw.get("data", raw.get("activities", []))
            else:
                items = []

            if not items:
                break

            for act in items:
                # Parse the activity timestamp
                ts_raw = act.get("createdAt") or act.get("sendAt") or act.get("at") or ""
                act_date = None
                if ts_raw:
                    try:
                        if isinstance(ts_raw, (int, float)):
                            act_date = datetime.fromtimestamp(ts_raw / 1000, tz=_tz_utc_module.utc)
                        else:
                            ts_str = str(ts_raw)
                            for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                                        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
                                try:
                                    act_date = datetime.strptime(ts_str[:26], fmt)
                                    if act_date.tzinfo is None:
                                        act_date = act_date.replace(tzinfo=_tz_utc_module.utc)
                                    break
                                except ValueError:
                                    continue
                    except Exception:
                        pass

                if act_date and start_dt <= act_date <= end_dt:
                    date_str = act_date.strftime("%Y-%m-%d")
                    status = (
                        act.get("callStatus")
                        or act.get("status")
                        or act.get("callResult")
                        or "unknown"
                    )
                    cam_calls.append({
                        "campaign_id": cam_id,
                        "date": date_str,
                        "lead_email": act.get("leadEmail") or act.get("email") or "",
                        "lead_name": (
                            (act.get("firstName") or "") + " " + (act.get("lastName") or "")
                        ).strip(),
                        "call_status": status,
                        "duration_seconds": act.get("callDuration") or act.get("duration") or 0,
                        "raw": act,
                    })

            # If we got fewer than page_size, no more pages
            if len(items) < page_size:
                break
            offset += page_size

        per_campaign_counts[cam_id] = len(cam_calls)
        all_calls.extend(cam_calls)

    # Build per-day summary
    daily: dict = defaultdict(lambda: {
        "total_calls": 0,
        "connected": 0,
        "voicemail": 0,
        "no_answer": 0,
        "other": 0,
    })

    _CONNECTED_STATUSES = {"connected", "answered", "in-progress", "completed"}
    _VOICEMAIL_STATUSES = {"voicemail", "left-voicemail", "left_voicemail", "voicemail-left"}
    _NO_ANSWER_STATUSES = {"no-answer", "no_answer", "noanswer", "missed", "not_answered"}

    for call in all_calls:
        d = call["date"]
        s = (call["call_status"] or "").lower().replace(" ", "_")
        daily[d]["total_calls"] += 1
        if s in _CONNECTED_STATUSES:
            daily[d]["connected"] += 1
        elif s in _VOICEMAIL_STATUSES:
            daily[d]["voicemail"] += 1
        elif s in _NO_ANSWER_STATUSES:
            daily[d]["no_answer"] += 1
        else:
            daily[d]["other"] += 1

    summary_table = [
        {"date": d, **counts}
        for d, counts in sorted(daily.items())
    ]

    # Strip raw field from all_calls for cleaner output
    clean_calls = [{k: v for k, v in c.items() if k != "raw"} for c in all_calls]

    note = (
        "Call status (connected/voicemail/no_answer) comes from the 'callStatus' field on each "
        "AirCall activity. If all statuses show as 'unknown', your Lemlist AirCall integration "
        "may not be passing status back — check AirCall webhook settings. "
        "Lemlist records when a call is INITIATED (aircallCreated); call outcomes are set by "
        "AirCall after the call ends."
    )

    return json.dumps({
        "status": "success",
        "period": {"start": start_date, "end": end_date},
        "campaigns_checked": len(ids),
        "total_calls_in_period": len(all_calls),
        "summary_table": summary_table,
        "per_campaign_breakdown": per_campaign_counts,
        "errors": errors,
        "note": note,
        "all_calls": clean_calls,
    }, indent=2, default=str)


_email_summary_cache: dict = {}
_EMAIL_SUMMARY_TTL = 3600  # 1 hour

# Lazy per-user-id name cache: avoids re-hitting /users/{id} for the same
# missing id across multiple tool calls within one process lifetime.
_user_name_cache: dict = {}


def _parse_date_param(s: str, end_of_day: bool = False):
    """Parse a date param that may be 'YYYY-MM-DD' OR an ISO-8601 timestamp
    like '2026-05-01T00:00:00Z'. Returns a UTC-aware datetime.

    When end_of_day=True and the input is a plain date (no time component),
    promotes to 23:59:59 so 'end_date' is inclusive of the whole day. ISO
    inputs are used as-given (the caller already specified the boundary)."""
    if s is None:
        raise ValueError("date is required")
    text = str(s).strip()
    if not text:
        raise ValueError("date is empty")
    # Plain YYYY-MM-DD fast path (matches existing user/agent contract).
    try:
        d = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=_tz_utc_module.utc)
        if end_of_day:
            d = d.replace(hour=23, minute=59, second=59)
        return d
    except ValueError:
        pass
    # ISO-8601 (handles trailing Z, fractional seconds, explicit offsets).
    try:
        iso = text.replace("Z", "+00:00")
        d = datetime.fromisoformat(iso)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_tz_utc_module.utc)
        return d
    except ValueError:
        pass
    raise ValueError(
        f"Invalid date '{s}'. Accepted formats: 'YYYY-MM-DD' "
        f"(e.g. '2026-05-01') OR ISO-8601 (e.g. '2026-05-01T00:00:00Z')."
    )


def _resolve_user_names_batch(uids):
    """For each uid not already in _user_name_cache, hit /users/{uid} once and
    cache the friendly name. Returns dict[uid -> {name, email}] for the
    requested uids. Network failures are swallowed (the uid stays in the
    cache as None so we don't retry on every call)."""
    out = {}
    for uid in uids:
        if not uid or uid == "unknown":
            continue
        if uid in _user_name_cache:
            cached = _user_name_cache[uid]
            if cached:
                out[uid] = cached
            continue
        try:
            u = _get(f"/users/{uid}") or {}
            if isinstance(u, list) and u:
                u = u[0]
            if not isinstance(u, dict):
                u = {}
            name = (
                u.get("name")
                or ((u.get("firstName") or "") + " " + (u.get("lastName") or "")).strip()
                or u.get("email")
                or ""
            )
            email = u.get("email", "") or ""
            if name:
                rec = {"name": name, "email": email}
                _user_name_cache[uid] = rec
                out[uid] = rec
            else:
                _user_name_cache[uid] = None
        except Exception:
            _user_name_cache[uid] = None
    return out


def _parse_activity_ts(ts_raw):
    if not ts_raw:
        return None
    try:
        if isinstance(ts_raw, (int, float)):
            return datetime.fromtimestamp(ts_raw / 1000, tz=_tz_utc_module.utc)
        ts_str = str(ts_raw).strip()
        # Prefer fromisoformat (handles fractional seconds + offsets robustly).
        try:
            iso = ts_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz_utc_module.utc)
            return dt
        except ValueError:
            pass
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(ts_str, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_tz_utc_module.utc)
                return dt
            except ValueError:
                continue
    except Exception:
        pass
    return None


@mcp.tool()
def lemlist_activity_summary_by_user(
    start_date: str,
    end_date: str,
    campaign_ids: Optional[str] = None,
) -> str:
    """
    Aggregate Lemlist email activity by BDR (sender) for a date range.

    This is the PRIMARY tool to answer questions like "how many emails did each
    BDR send last month?" or "show me opens / clicks / replies by sender for
    May". It paginates the /activities endpoint server-side across the 5 email
    event types (emailsSent, emailsOpened, emailsClicked, emailsReplied,
    emailsBounced), filters by date, joins against /team/senders to attach BDR
    names, and returns a small per-BDR table instead of thousands of raw rows.

    Results are cached for 1 hour by (start_date, end_date, campaign_ids).

    Args:
        start_date:   Start date inclusive, YYYY-MM-DD (e.g. "2026-05-01").
        end_date:     End date inclusive, YYYY-MM-DD (e.g. "2026-05-31").
        campaign_ids: Optional comma-separated campaign IDs to restrict the
                      aggregation, e.g. "cam_ABC,cam_DEF". If omitted,
                      aggregates across every campaign in the workspace.

    Returns:
        JSON with:
          - period: {start, end}
          - totals: {emails_sent, opened, clicked, replied, bounced}
          - by_bdr: list of {user_id, name, email, emails_sent, opened,
                             clicked, replied, replied_rate_pct, bounced},
                    sorted by emails_sent desc.
          - by_campaign: list of {campaign_id, sent, opened, clicked,
                                  replied, bounced} for the same period.
          - pages_fetched, activities_in_window, cached
    """
    from collections import defaultdict

    try:
        start_dt = _parse_date_param(start_date, end_of_day=False)
        end_dt = _parse_date_param(end_date, end_of_day=True)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)

    cam_filter = [c.strip() for c in (campaign_ids or "").split(",") if c.strip()]
    cache_key = (start_date, end_date, tuple(sorted(cam_filter)))
    now = time.time()
    cached = _email_summary_cache.get(cache_key)
    if cached and now - cached["_ts"] < _EMAIL_SUMMARY_TTL:
        out = dict(cached["payload"])
        out["cached"] = True
        return json.dumps(out, indent=2, default=str)

    # Build campaign -> {user_ids} map from /team/senders. A campaign can have
    # multiple senders, so we keep a set and only resolve a missing userId on
    # an activity when the set is unambiguous (exactly one sender).
    cam_to_users: dict = {}
    user_meta: dict = {}
    try:
        senders = _get("/team/senders") or []
        if isinstance(senders, dict):
            senders = senders.get("data", senders.get("senders", []))
        for s in senders or []:
            uid = s.get("userId") or s.get("_id") or s.get("id") or ""
            if not uid:
                continue
            user_meta[uid] = {
                "user_id": uid,
                "name": (
                    s.get("name")
                    or ((s.get("firstName") or "") + " " + (s.get("lastName") or "")).strip()
                    or s.get("email") or uid
                ),
                "email": s.get("email", ""),
            }
            for cam in s.get("campaigns", []) or []:
                cid = cam.get("_id") or cam.get("id") or cam.get("campaignId")
                if cid:
                    cam_to_users.setdefault(cid, set()).add(uid)
    except Exception as exc:
        return json.dumps({"error": f"Failed to load senders: {exc}"}, indent=2)
    user_meta["unknown"] = {"user_id": "unknown", "name": "(unattributed)", "email": ""}

    EMAIL_TYPES = ("emailsSent", "emailsOpened", "emailsClicked",
                   "emailsReplied", "emailsBounced")

    per_user: dict = defaultdict(lambda: {t: 0 for t in EMAIL_TYPES})
    per_campaign: dict = defaultdict(lambda: {t: 0 for t in EMAIL_TYPES})
    totals = {t: 0 for t in EMAIL_TYPES}
    pages_fetched = 0
    activities_in_window = 0
    errors: list = []
    truncated_streams: list = []  # (event_type, campaign_id_or_*) that hit MAX_PAGES
    PAGE = 100
    MAX_PAGES_PER_TYPE = 500  # hard ceiling (50k activities/type)

    for ev_type in EMAIL_TYPES:
        targets = cam_filter if cam_filter else [None]
        for cam_id in targets:
            offset = 0
            pages_for_this = 0
            stop = False
            while not stop and pages_for_this < MAX_PAGES_PER_TYPE:
                params: dict = {"type": ev_type, "offset": offset, "limit": PAGE}
                if cam_id:
                    params["campaignId"] = cam_id
                try:
                    raw = _get("/activities", params=params)
                except Exception as exc:
                    errors.append(f"{ev_type} cam={cam_id or '*'}: {exc}")
                    break
                pages_fetched += 1
                pages_for_this += 1
                if isinstance(raw, list):
                    items = raw
                elif isinstance(raw, dict):
                    items = raw.get("data", raw.get("activities", []))
                else:
                    items = []
                if not items:
                    break
                older_than_window = 0
                for act in items:
                    ts = _parse_activity_ts(
                        act.get("createdAt") or act.get("sendAt") or act.get("at")
                    )
                    if not ts:
                        continue
                    if ts < start_dt:
                        older_than_window += 1
                        continue
                    if ts > end_dt:
                        continue
                    activities_in_window += 1
                    uid = act.get("userId") or act.get("sendUserId") or act.get("senderId") or ""
                    cid = act.get("campaignId") or cam_id or ""
                    if not uid and cid:
                        # Only trust the campaign->user fallback if the campaign
                        # has exactly one known sender; otherwise mark unattributed.
                        candidates = cam_to_users.get(cid, set())
                        if len(candidates) == 1:
                            uid = next(iter(candidates))
                    if not uid:
                        uid = "unknown"
                    per_user[uid][ev_type] += 1
                    if uid not in user_meta:
                        user_meta[uid] = {"user_id": uid, "name": uid, "email": ""}
                    if cid:
                        per_campaign[cid][ev_type] += 1
                    totals[ev_type] += 1
                # Reverse-chronological: once we see all rows older than start, stop
                if older_than_window == len(items):
                    stop = True
                    break
                if len(items) < PAGE:
                    break
                offset += PAGE
            if pages_for_this >= MAX_PAGES_PER_TYPE and not stop:
                truncated_streams.append({
                    "event_type": ev_type,
                    "campaign_id": cam_id or "*",
                    "pages_fetched": pages_for_this,
                })

    # Batch-resolve names for any uid that ended up with name==uid (i.e. usr_xxx).
    # These are users not in /team/senders for this workspace — usually because
    # they no longer have an active sending channel. /users/{uid} still returns
    # their profile, which is what callers actually want to see.
    unresolved = [
        uid for uid in per_user.keys()
        if uid != "unknown" and user_meta.get(uid, {}).get("name") == uid
    ]
    if unresolved:
        resolved = _resolve_user_names_batch(unresolved)
        for uid, rec in resolved.items():
            user_meta[uid] = {"user_id": uid, "name": rec["name"], "email": rec["email"]}

    by_bdr = []
    for uid, counts in per_user.items():
        sent = counts["emailsSent"]
        replied = counts["emailsReplied"]
        meta = user_meta.get(uid, {"user_id": uid, "name": uid, "email": ""})
        by_bdr.append({
            "user_id": uid,
            "name": meta.get("name", uid),
            "email": meta.get("email", ""),
            "emails_sent": sent,
            "opened": counts["emailsOpened"],
            "clicked": counts["emailsClicked"],
            "replied": replied,
            "replied_rate_pct": (round(replied / sent * 100, 1) if sent else 0.0),
            "bounced": counts["emailsBounced"],
        })
    by_bdr.sort(key=lambda r: r["emails_sent"], reverse=True)

    by_campaign = [
        {
            "campaign_id": cid,
            "sent": c["emailsSent"], "opened": c["emailsOpened"],
            "clicked": c["emailsClicked"], "replied": c["emailsReplied"],
            "bounced": c["emailsBounced"],
        }
        for cid, c in sorted(per_campaign.items(), key=lambda kv: kv[1]["emailsSent"], reverse=True)
    ]

    is_partial = bool(errors) or bool(truncated_streams)
    payload = {
        "status": "partial" if is_partial else "success",
        "partial": is_partial,
        "period": {"start": start_date, "end": end_date},
        "campaign_filter": cam_filter or None,
        "totals": {
            "emails_sent": totals["emailsSent"],
            "opened": totals["emailsOpened"],
            "clicked": totals["emailsClicked"],
            "replied": totals["emailsReplied"],
            "bounced": totals["emailsBounced"],
        },
        "by_bdr": by_bdr,
        "by_campaign": by_campaign,
        "pages_fetched": pages_fetched,
        "activities_in_window": activities_in_window,
        "errors": errors,
        "truncated_streams": truncated_streams,
        "cached": False,
        "note": (
            "Lemlist's /activities endpoint has no native date filter, so this tool paginates "
            "from newest backwards and stops when all rows on a page are older than start_date. "
            "First call for a given (start, end) takes 30-90s for a full month; complete "
            "(non-partial) results are cached for 1 hour. If status == 'partial', counts may "
            "be undercounted — inspect 'errors' and 'truncated_streams' before reporting."
        ),
    }
    # Only cache complete, error-free results to avoid serving stale partial data.
    if not is_partial:
        _email_summary_cache[cache_key] = {"_ts": now, "payload": payload}
    return json.dumps(payload, indent=2, default=str)


@mcp.tool()
def lemlist_get_unsubscribes() -> str:
    """
    Get all globally unsubscribed email addresses.

    Returns:
        JSON string with array of unsubscribed email objects.
    """
    result = _get("/unsubscribes")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_add_unsubscribe(email: str) -> str:
    """
    Add an email to the global unsubscribe list.

    Args:
        email: The email address to unsubscribe.

    Returns:
        JSON string confirming the addition.
    """
    result = _post("/unsubscribes", {"email": email})
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_remove_unsubscribe(email: str) -> str:
    """
    Remove an email from the global unsubscribe list.

    Args:
        email: The email address to re-subscribe.

    Returns:
        JSON string confirming the removal.
    """
    result = _delete(f"/unsubscribes/{email}")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_unsubscribe_lead_from_campaign(campaign_id: str, email: str) -> str:
    """
    Unsubscribe a lead from a specific campaign. Note: this unsubscribes them
    from ALL campaigns in the workspace.

    Args:
        campaign_id: The campaign's ID.
        email: The lead's email address.

    Returns:
        JSON string confirming unsubscription.
    """
    result = _post(f"/campaigns/{campaign_id}/leads/{email}/unsubscribe")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_send_email(
    from_email: str,
    to_email: str,
    subject: str,
    html: Optional[str] = None,
    text: Optional[str] = None,
) -> str:
    """
    Send an email through lemlist's inbox.

    Args:
        from_email: Sender email address (must be connected in lemlist).
        to_email: Recipient email address.
        subject: Email subject line.
        html: HTML body of the email.
        text: Plain text body of the email (used if html not provided).

    Returns:
        JSON string with send confirmation.
    """
    payload: dict = {
        "from": from_email,
        "to": to_email,
        "subject": subject,
    }
    if html:
        payload["html"] = html
    if text:
        payload["text"] = text
    result = _post("/inbox/send", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_get_schedules() -> str:
    """
    List all sending schedules configured in the workspace.

    Returns:
        JSON string with array of schedule objects.
    """
    result = _get("/schedules")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_list_webhooks() -> str:
    """
    List all configured webhooks.

    Returns:
        JSON string with array of webhook objects.
    """
    result = _get("/webhooks")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_create_webhook(
    target_url: str,
    event: str,
    campaign_id: Optional[str] = None,
) -> str:
    """
    Create a new webhook to receive notifications for specific events.

    Args:
        target_url: The URL that will receive webhook POST requests.
        event: The event type to listen for. Options include:
            "emailsSent", "emailsOpened", "emailsClicked", "emailsReplied",
            "emailsBounced", "emailsFailed", "emailsUnsubscribed",
            "linkedinInviteSent", "linkedinReplied", "linkedinAccepted",
            "interested", "notInterested", "meetingBooked", "taskCompleted"
        campaign_id: Optional campaign ID to scope the webhook to a specific campaign.

    Returns:
        JSON string with the created webhook details.
    """
    payload: dict = {"targetUrl": target_url, "type": event}
    if campaign_id:
        payload["campaignId"] = campaign_id
    result = _post("/webhooks", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_delete_webhook(webhook_id: str) -> str:
    """
    Delete a webhook by its ID.

    Args:
        webhook_id: The webhook's ID.

    Returns:
        JSON string confirming deletion.
    """
    result = _delete(f"/webhooks/{webhook_id}")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_search_people(
    query: Optional[str] = None,
    job_titles: Optional[List[str]] = None,
    locations: Optional[List[str]] = None,
    industries: Optional[List[str]] = None,
    company_sizes: Optional[List[str]] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> str:
    """
    Search the lemlist People database for contacts matching filters.

    Args:
        query: Free-text search query.
        job_titles: Filter by job titles, e.g. ["VP of Sales", "CEO"].
        locations: Filter by locations, e.g. ["San Francisco", "New York"].
        industries: Filter by industries, e.g. ["Software", "Finance"].
        company_sizes: Filter by company size ranges, e.g. ["1-10", "11-50"].
        limit: Maximum results to return.
        offset: Number of results to skip (pagination).

    Returns:
        JSON string with matching people results.
    """
    payload: dict = {}
    if query:
        payload["query"] = query
    if job_titles:
        payload["jobTitles"] = job_titles
    if locations:
        payload["locations"] = locations
    if industries:
        payload["industries"] = industries
    if company_sizes:
        payload["companySizes"] = company_sizes
    if limit is not None:
        payload["limit"] = limit
    if offset is not None:
        payload["offset"] = offset
    result = _post("/people/search", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_enrich_lead(
    email: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    linkedin_url: Optional[str] = None,
    company_name: Optional[str] = None,
    company_domain: Optional[str] = None,
    find_email: bool = True,
    find_phone: bool = True,
    linkedin_enrichment: bool = True,
    verify_email: bool = False,
) -> str:
    """
    Enrich a person's data using Lemlist's enrichment service. Finds emails,
    phone numbers, and LinkedIn profile data. This is an async API — the tool
    polls for results automatically (up to 30 seconds).

    You must provide at least one of: email, linkedin_url, or (first_name + last_name + company_name).

    Args:
        email: The email address to enrich.
        first_name: Person's first name (used with last_name + company for lookup).
        last_name: Person's last name.
        linkedin_url: LinkedIn profile URL for enrichment.
        company_name: Company name (used with first/last name for lookup).
        company_domain: Company domain (alternative to company_name).
        find_email: Whether to find/verify the email address (default True).
        find_phone: Whether to find phone number (default True).
        linkedin_enrichment: Whether to enrich LinkedIn profile data (default True).
        verify_email: Whether to verify email deliverability (default False).

    Returns:
        JSON string with enriched data including email, phone, LinkedIn profile,
        work history, company details, etc. Returns enrichment ID if results
        are not ready within 30 seconds — use lemlist_get_enrichment_result to poll.
    """
    params = {}
    if email:
        params["email"] = email
    if first_name:
        params["firstName"] = first_name
    if last_name:
        params["lastName"] = last_name
    if linkedin_url:
        params["linkedinUrl"] = linkedin_url
    if company_name:
        params["companyName"] = company_name
    if company_domain:
        params["companyDomain"] = company_domain
    if find_email:
        params["findEmail"] = "true"
    if find_phone:
        params["findPhone"] = "true"
    if linkedin_enrichment:
        params["linkedinEnrichment"] = "true"
    if verify_email:
        params["verifyEmail"] = "true"

    has_email = "email" in params
    has_linkedin = "linkedinUrl" in params
    has_name_combo = (
        "firstName" in params
        and "lastName" in params
        and ("companyName" in params or "companyDomain" in params)
    )
    if not (has_email or has_linkedin or has_name_combo):
        raise RuntimeError(
            "Provide at least one of: email, linkedin_url, or (first_name + last_name + company_name/company_domain)."
        )

    url = f"{BASE_URL}/enrich"
    resp = _request_with_retry("POST", url, _headers(), params=params)
    _raise_with_detail(resp)
    if not resp.text or not resp.text.strip():
        raise RuntimeError("Lemlist enrichment returned empty response.")
    init_result = resp.json()

    enrichment_id = init_result.get("id")
    if not enrichment_id:
        return json.dumps(init_result, indent=2)

    for attempt in range(10):
        time.sleep(3)
        poll_resp = _request_with_retry("GET", f"{BASE_URL}/enrich/{enrichment_id}", _headers())
        if poll_resp.status_code == 202:
            continue
        if poll_resp.status_code == 200 and poll_resp.text and poll_resp.text.strip():
            return json.dumps(poll_resp.json(), indent=2)
        if poll_resp.status_code >= 400:
            _raise_with_detail(poll_resp)

    return json.dumps({
        "status": "in_progress",
        "enrichment_id": enrichment_id,
        "message": "Enrichment still processing after 30 seconds. Call lemlist_get_enrichment_result with this enrichment_id to get results later."
    }, indent=2)


@mcp.tool()
def lemlist_get_enrichment_result(enrichment_id: str) -> str:
    """
    Get the result of a Lemlist enrichment request. Use this when lemlist_enrich_lead
    returned an enrichment_id with status 'in_progress'.

    Args:
        enrichment_id: The enrichment ID (e.g. 'enr_xxx') returned by lemlist_enrich_lead.

    Returns:
        JSON string with enriched data if complete, or status 'in_progress' if still processing.
    """
    url = f"{BASE_URL}/enrich/{enrichment_id}"
    resp = _request_with_retry("GET", url, _headers())
    if resp.status_code == 202:
        return json.dumps({
            "status": "in_progress",
            "enrichment_id": enrichment_id,
            "message": "Enrichment is still processing. Wait a few seconds and try again."
        }, indent=2)
    _raise_with_detail(resp)
    if not resp.text or not resp.text.strip():
        return json.dumps({
            "status": "in_progress",
            "enrichment_id": enrichment_id,
            "message": "No data yet. Wait a few seconds and try again."
        }, indent=2)
    return json.dumps(resp.json(), indent=2)


@mcp.tool()
def lemlist_get_tasks(
    assignee_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> str:
    """
    Get manual tasks assigned to a specific team member (user).

    IMPORTANT: The Lemlist /tasks API only accepts assigneeId as a filter.
    It does NOT support campaignId or status filtering — passing those causes
    a 400 "Malformed filters" error. To find pending CALL tasks per campaign,
    use lemlist_get_pending_call_tasks instead.

    Args:
        assignee_id: Lemlist user ID (e.g. "usr_abc123") to filter tasks by assignee.
                     Get user IDs from lemlist_get_users. If omitted, may return all
                     tasks or require a valid assignee depending on account settings.
        limit:       Maximum number of tasks to return.

    Returns:
        JSON string with array of task objects.
    """
    params = {}
    if assignee_id:
        params["assigneeId"] = assignee_id
    if limit is not None:
        params["limit"] = limit
    result = _get("/tasks", params=params if params else None)
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_complete_task(task_id: str) -> str:
    """
    Mark a task as completed.

    Args:
        task_id: The task's ID.

    Returns:
        JSON string confirming task completion.
    """
    result = _post(f"/tasks/{task_id}/complete")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_get_pending_call_tasks(
    campaign_ids: str,
) -> str:
    """
    Get the count of pending call tasks per campaign across one or more campaigns.

    This is the PRIMARY tool to use when asked about "pending calls", "pending call tasks",
    or "how many calls are left to make" per campaign.

    In Lemlist, a "pending call task" = a lead whose current state is "aircall" — meaning
    they are sitting at a call step in the sequence that has NOT yet been executed.
    This is determined by fetching leads per campaign and counting those with state="aircall".

    The Lemlist /tasks API does NOT support campaign or status filtering (returns
    "Malformed filters" error). This tool uses the correct approach via campaign leads.

    Args:
        campaign_ids: Comma-separated campaign IDs, e.g.
                      "cam_ABC123,cam_DEF456,cam_GHI789"

    Returns:
        JSON with a summary table (campaign_id → pending_call_count) and any errors.
    """
    ids = [c.strip() for c in campaign_ids.split(",") if c.strip()]
    if not ids:
        return json.dumps({"error": "No campaign IDs provided."}, indent=2)

    results = []
    errors = []

    for cam_id in ids:
        pending_count = 0
        offset = 0
        page_size = 100
        while True:
            params: dict = {"limit": page_size, "offset": offset}
            try:
                raw = _get(f"/campaigns/{cam_id}/leads", params=params)
            except Exception as exc:
                errors.append({"campaign_id": cam_id, "error": str(exc)})
                break

            if isinstance(raw, list):
                items = raw
            elif isinstance(raw, dict):
                items = raw.get("data", raw.get("leads", []))
            else:
                items = []

            if not items:
                break

            for lead in items:
                state = (lead.get("state") or "").lower()
                # "aircall" = lead is currently at a call step awaiting execution
                if state == "aircall":
                    pending_count += 1

            if len(items) < page_size:
                break
            offset += page_size

        results.append({
            "campaign_id": cam_id,
            "pending_call_tasks": pending_count,
        })

    total = sum(r["pending_call_tasks"] for r in results)

    return json.dumps({
        "status": "success",
        "campaigns_checked": len(ids),
        "total_pending_call_tasks": total,
        "note": (
            "pending_call_tasks = leads currently at a call step (state='aircall') "
            "that have not yet been called. This excludes leads with state='aircallDone' "
            "(call completed) or any other state."
        ),
        "summary_table": results,
        "errors": errors,
    }, indent=2)


@mcp.tool()
def lemlist_get_campaign_leads(
    campaign_id: str,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    status: Optional[str] = None,
) -> str:
    """
    List all leads in a specific campaign.

    Args:
        campaign_id: The campaign's ID.
        offset: Number of leads to skip (pagination).
        limit: Maximum number of leads to return.
        status: Filter by lead status.

    Returns:
        JSON string with array of lead objects in the campaign.
    """
    params = {}
    if offset is not None:
        params["offset"] = offset
    if limit is not None:
        params["limit"] = limit
    if status:
        params["status"] = status
    result = _get(f"/campaigns/{campaign_id}/leads", params=params if params else None)
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_get_campaign_sequences(campaign_id: str) -> str:
    """
    Get the sequence steps of a campaign (email steps, LinkedIn steps, delays, etc.).

    Args:
        campaign_id: The campaign's ID.

    Returns:
        JSON string with the campaign's sequence configuration.
    """
    result = _get(f"/campaigns/{campaign_id}/sequences")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_create_campaign(
    name: str,
    schedule_id: Optional[str] = None,
) -> str:
    """
    Create a new campaign.

    Args:
        name: Name for the new campaign.
        schedule_id: Optional schedule ID to assign.

    Returns:
        JSON string with the created campaign details including its ID.
    """
    payload: dict = {"name": name}
    if schedule_id:
        payload["scheduleId"] = schedule_id
    result = _post("/campaigns", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_update_campaign(
    campaign_id: str,
    name: Optional[str] = None,
    schedule_id: Optional[str] = None,
) -> str:
    """
    Update a campaign's settings.

    Args:
        campaign_id: The campaign's ID.
        name: New name for the campaign.
        schedule_id: New schedule ID to assign.

    Returns:
        JSON string with updated campaign details.
    """
    payload: dict = {}
    if name:
        payload["name"] = name
    if schedule_id:
        payload["scheduleId"] = schedule_id
    result = _patch(f"/campaigns/{campaign_id}", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_get_crm_integrations() -> str:
    """
    Get information about CRM integrations connected to the workspace.

    Returns:
        JSON string with CRM integration details.
    """
    result = _get("/crms")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_add_leads_batch(
    campaign_id: str,
    leads: List[dict],
    deduplicate: bool = True,
) -> str:
    """
    Add multiple leads to a campaign in a single request.

    Args:
        campaign_id: The campaign's ID.
        leads: List of lead dicts. Each must have "email" and can include:
            firstName, lastName, companyName, phone, linkedinUrl, icebreaker,
            and any custom fields as additional keys.
        deduplicate: If True (default), skip leads that already exist in the campaign.

    Returns:
        JSON string with the batch import result.
    """
    params_str = "?deduplicate=true" if deduplicate else ""
    url_path = f"/campaigns/{campaign_id}/leads/batch{params_str}"
    added_date = datetime.now(_tz_utc_module.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")

    clean_leads = []
    for lead in leads:
        cleaned = dict(lead)
        if "firstName" in cleaned and cleaned["firstName"]:
            cleaned["firstName"] = _clean_first_name(
                cleaned["firstName"], cleaned.get("lastName", "")
            )
        clean_leads.append(cleaned)
    leads = clean_leads

    def _sync_batch_to_sf(lead_list):
        for lead in lead_list:
            lead_email = lead.get("email", "")
            if lead_email:
                _sync_campaign_date_to_salesforce(lead_email, added_date)

    try:
        result = _post(url_path, leads)
        _sync_batch_to_sf(leads)
        return json.dumps(result, indent=2)
    except RuntimeError as e:
        if "500" not in str(e):
            raise

        was_running = False
        try:
            camp_info = _get(f"/campaigns/{campaign_id}")
            was_running = camp_info.get("state") == "running" or camp_info.get("status") == "running"
        except Exception:
            pass

        if was_running:
            try:
                _post(f"/campaigns/{campaign_id}/pause")
            except Exception:
                pass
            time.sleep(2)

        for retry in range(3):
            try:
                result = _post(url_path, leads)
                if was_running:
                    try:
                        _post(f"/campaigns/{campaign_id}/start")
                    except Exception:
                        pass
                _sync_batch_to_sf(leads)
                return json.dumps(result, indent=2)
            except RuntimeError as retry_err:
                if "500" in str(retry_err) and retry < 2:
                    time.sleep(3 * (retry + 1))
                    continue
                if was_running:
                    try:
                        _post(f"/campaigns/{campaign_id}/start")
                    except Exception:
                        pass
                raise RuntimeError(
                    f"Lemlist 500 error persists after pausing campaign and {retry + 1} retries. "
                    f"Campaign ID: {campaign_id}. "
                    f"Try: 1) Duplicate the campaign in Lemlist UI and use the new campaign ID, "
                    f"or 2) Contact Lemlist support about campaign {campaign_id}. "
                    f"Original error: {retry_err}"
                )


@mcp.tool()
def lemlist_resume_lead_in_campaign(campaign_id: str, email: str) -> str:
    """
    Resume a paused lead in a campaign.

    Args:
        campaign_id: The campaign's ID.
        email: The lead's email address.

    Returns:
        JSON string confirming the lead was resumed.
    """
    result = _post(f"/campaigns/{campaign_id}/leads/{email}/resume")
    return json.dumps(result, indent=2)


@mcp.tool()
def lemlist_pause_lead_in_campaign(campaign_id: str, email: str) -> str:
    """
    Pause a lead in a specific campaign.

    Args:
        campaign_id: The campaign's ID.
        email: The lead's email address.

    Returns:
        JSON string confirming the lead was paused.
    """
    result = _post(f"/campaigns/{campaign_id}/leads/{email}/pause")
    return json.dumps(result, indent=2)


def _paginate_activities(campaign_id, ev_type, start_dt, end_dt,
                         max_pages=200, page_size=100):
    """Paginate /activities for one event type + campaign, filter by date window,
    and return (list_of_activity_dicts, truncated_bool, error_str_or_None).

    Mirrors the pattern used in lemlist_activity_summary_by_user: stops early
    once a whole page is older than start_dt, and respects a hard page ceiling
    so a runaway campaign can't hang the agent."""
    out = []
    offset = 0
    pages = 0
    truncated = False
    while pages < max_pages:
        params = {"type": ev_type, "offset": offset, "limit": page_size}
        if campaign_id:
            params["campaignId"] = campaign_id
        try:
            raw = _get("/activities", params=params)
        except Exception as exc:
            return out, truncated, f"{ev_type}: {exc}"
        pages += 1
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = raw.get("data", raw.get("activities", []))
        else:
            items = []
        if not items:
            break
        older_than_window = 0
        for act in items:
            ts = _parse_activity_ts(
                act.get("createdAt") or act.get("sendAt") or act.get("at")
            )
            if not ts:
                continue
            if ts < start_dt:
                older_than_window += 1
                continue
            if ts > end_dt:
                continue
            act["_ts"] = ts
            out.append(act)
        if older_than_window == len(items):
            break
        if len(items) < page_size:
            break
        offset += page_size
    if pages >= max_pages:
        truncated = True
    return out, truncated, None


def _classify_bounce(act):
    """Return one of: 'hard', 'soft', 'invalid', 'catch_all', 'other'.

    Lemlist exposes inconsistent bounce metadata depending on the SMTP path,
    so we check several fields and fall back to SMTP code heuristics."""
    btype = (act.get("bounceType") or act.get("type2") or "").lower()
    reason = (act.get("bounceReason") or act.get("reason") or act.get("error") or "").lower()
    code = str(act.get("smtpCode") or act.get("code") or act.get("errorCode") or "")

    if "hard" in btype:
        return "hard"
    if "soft" in btype:
        return "soft"
    if "invalid" in btype or "invalid" in reason or "syntax" in reason:
        return "invalid"
    if "catch" in btype or "catch" in reason or "catchall" in reason or "catch-all" in reason:
        return "catch_all"
    # SMTP code heuristic: 5xx = hard, 4xx = soft.
    if code.startswith("5"):
        return "hard"
    if code.startswith("4"):
        return "soft"
    # Keyword sniff on the reason string as a last resort.
    if "does not exist" in reason or "no such user" in reason or "unknown user" in reason:
        return "hard"
    if "mailbox full" in reason or "quota" in reason or "temporarily" in reason or "try again" in reason:
        return "soft"
    return "other"


@mcp.tool()
def lemlist_bounce_breakdown(
    campaign_id: str,
    start_date: str,
    end_date: str,
) -> str:
    """
    Categorize email bounces for a campaign over a date range into hard / soft
    / invalid-syntax / catch-all / other.

    Use this to answer: "is my bounce rate driven by bad list hygiene
    (invalid/hard) or by deliverability/warm-up issues (soft/catch-all)?"

    Args:
        campaign_id: The Lemlist campaign ID (e.g. "cam_ABC123").
        start_date:  Inclusive start, 'YYYY-MM-DD' or ISO-8601.
        end_date:    Inclusive end, 'YYYY-MM-DD' or ISO-8601.

    Returns:
        JSON with:
          - period, campaign_id, total_bounces
          - breakdown: {hard, soft, invalid, catch_all, other}
          - sample_emails: up to 20 example bounces per category
            ({lead_email, category, reason, smtp_code})
          - truncated, error
    """
    from collections import defaultdict
    try:
        start_dt = _parse_date_param(start_date, end_of_day=False)
        end_dt = _parse_date_param(end_date, end_of_day=True)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)

    acts, truncated, err = _paginate_activities(
        campaign_id, "emailsBounced", start_dt, end_dt
    )

    counts = defaultdict(int)
    samples: dict = defaultdict(list)
    for a in acts:
        cat = _classify_bounce(a)
        counts[cat] += 1
        if len(samples[cat]) < 20:
            samples[cat].append({
                "lead_email": a.get("leadEmail") or a.get("email") or "",
                "category": cat,
                "reason": a.get("bounceReason") or a.get("reason") or a.get("error") or "",
                "smtp_code": a.get("smtpCode") or a.get("code") or a.get("errorCode") or "",
                "at": (a["_ts"].isoformat() if a.get("_ts") else ""),
            })

    return json.dumps({
        "status": "partial" if (truncated or err) else "success",
        "period": {"start": start_date, "end": end_date},
        "campaign_id": campaign_id,
        "total_bounces": len(acts),
        "breakdown": {
            "hard": counts.get("hard", 0),
            "soft": counts.get("soft", 0),
            "invalid": counts.get("invalid", 0),
            "catch_all": counts.get("catch_all", 0),
            "other": counts.get("other", 0),
        },
        "sample_emails": dict(samples),
        "truncated": truncated,
        "error": err,
        "note": (
            "Bounce sub-type comes from Lemlist's bounceType/bounceReason/SMTP "
            "code when available. 'other' means Lemlist didn't expose enough "
            "detail to classify — for those rows, inspect the raw activity. "
            "Heuristic: high 'invalid' + 'hard' rate => list verification "
            "needed; high 'soft' + 'catch_all' rate => domain warm-up / "
            "reputation issue."
        ),
    }, indent=2, default=str)


def _extract_sender_email(act):
    """Pull the sending mailbox email from a Lemlist activity, trying the
    several field shapes the API uses across event types."""
    return (
        act.get("sendingEmail")
        or act.get("sendUserEmail")
        or act.get("senderEmail")
        or act.get("channelEmail")
        or act.get("from")
        or act.get("emailFrom")
        or ""
    )


@mcp.tool()
def lemlist_sender_performance(
    campaign_id: str,
    start_date: str,
    end_date: str,
    bounce_rate_threshold_pct: float = 5.0,
) -> str:
    """
    Per-sending-mailbox stats for a campaign: which inbox sent what, and which
    inboxes are bouncing. Deliverability problems often live in one bad inbox
    rather than the whole campaign.

    Args:
        campaign_id: The Lemlist campaign ID.
        start_date:  Inclusive start, 'YYYY-MM-DD' or ISO-8601.
        end_date:    Inclusive end, 'YYYY-MM-DD' or ISO-8601.
        bounce_rate_threshold_pct: Mailbox is flagged 'unhealthy' when
            bounce_rate_pct >= this value. Default 5.0.

    Returns:
        JSON with:
          - period, campaign_id, totals
          - by_sender: list of {sender_email, sent, delivered, opened, clicked,
                                replied, bounced, bounce_rate_pct, healthy}
            sorted by sent desc. 'delivered' = sent - bounced.
          - unhealthy_senders: those above the threshold
    """
    from collections import defaultdict
    try:
        start_dt = _parse_date_param(start_date, end_of_day=False)
        end_dt = _parse_date_param(end_date, end_of_day=True)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)

    EV = ("emailsSent", "emailsOpened", "emailsClicked", "emailsReplied", "emailsBounced")
    per_sender: dict = defaultdict(lambda: {t: 0 for t in EV})
    errors = []
    truncated_any = False
    for ev in EV:
        acts, truncated, err = _paginate_activities(campaign_id, ev, start_dt, end_dt)
        if err:
            errors.append(err)
        if truncated:
            truncated_any = True
        for a in acts:
            email = _extract_sender_email(a) or "(unknown sender)"
            per_sender[email][ev] += 1

    by_sender = []
    for email, c in per_sender.items():
        sent = c["emailsSent"]
        bounced = c["emailsBounced"]
        delivered = max(sent - bounced, 0)
        rate = round(bounced / sent * 100, 2) if sent else 0.0
        by_sender.append({
            "sender_email": email,
            "sent": sent,
            "delivered": delivered,
            "opened": c["emailsOpened"],
            "clicked": c["emailsClicked"],
            "replied": c["emailsReplied"],
            "bounced": bounced,
            "bounce_rate_pct": rate,
            "healthy": rate < bounce_rate_threshold_pct or sent < 20,
        })
    by_sender.sort(key=lambda r: r["sent"], reverse=True)
    unhealthy = [s for s in by_sender if not s["healthy"]]

    totals = {k: sum(c[k] for c in per_sender.values()) for k in EV}
    return json.dumps({
        "status": "partial" if (truncated_any or errors) else "success",
        "period": {"start": start_date, "end": end_date},
        "campaign_id": campaign_id,
        "threshold_pct": bounce_rate_threshold_pct,
        "totals": {
            "sent": totals["emailsSent"], "opened": totals["emailsOpened"],
            "clicked": totals["emailsClicked"], "replied": totals["emailsReplied"],
            "bounced": totals["emailsBounced"],
        },
        "by_sender": by_sender,
        "unhealthy_senders": unhealthy,
        "truncated": truncated_any,
        "errors": errors,
        "note": (
            "Sender email is pulled from Lemlist's sendingEmail / sendUserEmail "
            "/ channelEmail / from fields, whichever the activity provides. "
            "If many rows land under '(unknown sender)', the campaign may be "
            "using a routing pool where Lemlist doesn't stamp the mailbox per "
            "activity — switch to per-channel reporting in the UI for those. "
            "'healthy' is only computed once a sender has >= 20 sends so a "
            "single early bounce doesn't flag a fresh mailbox."
        ),
    }, indent=2, default=str)


def _extract_step_number(act):
    """Pull the sequence step number from an activity, defensively."""
    for k in ("step", "stepIndex", "stepNumber", "sequenceStep", "stepN"):
        v = act.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


@mcp.tool()
def lemlist_step_breakdown(
    campaign_id: str,
    start_date: str,
    end_date: str,
) -> str:
    """
    Per-sequence-step funnel for a campaign over a date range.
    Tells you whether step 1 is doing all the work or if the follow-ups
    (steps 2, 3, ...) are pulling weight.

    Args:
        campaign_id: The Lemlist campaign ID.
        start_date:  Inclusive start, 'YYYY-MM-DD' or ISO-8601.
        end_date:    Inclusive end, 'YYYY-MM-DD' or ISO-8601.

    Returns:
        JSON with:
          - period, campaign_id
          - steps: list of {step, subject, sent, opened, clicked, replied,
                            bounced, reply_rate_pct, open_rate_pct} sorted by step
          - totals across steps
    """
    from collections import defaultdict
    try:
        start_dt = _parse_date_param(start_date, end_of_day=False)
        end_dt = _parse_date_param(end_date, end_of_day=True)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)

    # Fetch campaign sequence so we can label steps by subject line.
    step_meta: dict = {}
    try:
        cam = _get(f"/campaigns/{campaign_id}") or {}
        for idx, s in enumerate(cam.get("steps") or cam.get("sequence") or [], start=1):
            n = s.get("step") if isinstance(s.get("step"), int) else idx
            step_meta[n] = {
                "subject": s.get("subject") or s.get("subjectTemplate") or "",
                "type": s.get("type") or s.get("channelType") or "",
            }
    except Exception:
        pass  # Step-meta is best-effort; the per-step counts still work without it.

    EV = ("emailsSent", "emailsOpened", "emailsClicked", "emailsReplied", "emailsBounced")
    per_step: dict = defaultdict(lambda: {t: 0 for t in EV})
    unknown_step = {t: 0 for t in EV}
    errors = []
    truncated_any = False
    for ev in EV:
        acts, truncated, err = _paginate_activities(campaign_id, ev, start_dt, end_dt)
        if err:
            errors.append(err)
        if truncated:
            truncated_any = True
        for a in acts:
            n = _extract_step_number(a)
            if n is None:
                unknown_step[ev] += 1
            else:
                per_step[n][ev] += 1

    steps_out = []
    for n in sorted(per_step.keys()):
        c = per_step[n]
        sent = c["emailsSent"]
        steps_out.append({
            "step": n,
            "subject": step_meta.get(n, {}).get("subject", ""),
            "type": step_meta.get(n, {}).get("type", ""),
            "sent": sent,
            "opened": c["emailsOpened"],
            "clicked": c["emailsClicked"],
            "replied": c["emailsReplied"],
            "bounced": c["emailsBounced"],
            "open_rate_pct": round(c["emailsOpened"] / sent * 100, 2) if sent else 0.0,
            "reply_rate_pct": round(c["emailsReplied"] / sent * 100, 2) if sent else 0.0,
        })

    totals = {k: sum(c[k] for c in per_step.values()) + unknown_step[k] for k in EV}
    return json.dumps({
        "status": "partial" if (truncated_any or errors) else "success",
        "period": {"start": start_date, "end": end_date},
        "campaign_id": campaign_id,
        "steps": steps_out,
        "unknown_step_counts": unknown_step,
        "totals": {
            "sent": totals["emailsSent"], "opened": totals["emailsOpened"],
            "clicked": totals["emailsClicked"], "replied": totals["emailsReplied"],
            "bounced": totals["emailsBounced"],
        },
        "truncated": truncated_any,
        "errors": errors,
        "note": (
            "Step number is read from the activity's step/stepIndex/stepNumber "
            "field — if Lemlist didn't tag a row, it lands in "
            "'unknown_step_counts'. Subject lines are joined from "
            "GET /campaigns/{id}; if the campaign was edited after these "
            "activities fired, the subject is the CURRENT one, not the "
            "historical one sent."
        ),
    }, indent=2, default=str)


def _extract_reply_text(act):
    """Pull the reply body / subject from a Lemlist 'emailsReplied' activity.
    Lemlist's field name is inconsistent — try the common ones."""
    body = (
        act.get("replyText") or act.get("text") or act.get("body")
        or act.get("content") or act.get("message") or act.get("replyContent")
        or ""
    )
    subject = act.get("subject") or act.get("replySubject") or ""
    return str(subject), str(body)


_REPLY_SYSTEM_PROMPT = (
    "You classify cold-outreach email replies into exactly ONE of these categories:\n"
    "  positive    - prospect is interested, wants to talk, asks for a meeting/demo/info, "
    "or any reply that moves a deal forward.\n"
    "  negative    - prospect is not interested, declines, says wrong person with no referral, "
    "or asks to stop contacting.\n"
    "  ooo         - out-of-office / vacation auto-reply / temporary absence.\n"
    "  unsubscribe - explicit unsubscribe / GDPR / opt-out / remove-me request.\n"
    "  referral    - prospect forwards or names a colleague who is the right contact.\n"
    "  other       - none of the above (newsletter, bounce-like noise, blank).\n"
    "Respond with ONLY a JSON object: {\"category\": \"<one of above>\", "
    "\"confidence\": 0.0-1.0, \"reason\": \"<<= 12 words>\"}. No prose."
)


@mcp.tool()
def lemlist_classify_replies(
    campaign_id: str,
    start_date: str,
    end_date: str,
    limit: int = 50,
) -> str:
    """
    Pull email replies for a campaign over a date range and classify each as
    positive / negative / ooo / unsubscribe / referral / other using Claude
    Haiku. This turns Lemlist's opaque "reply count" into actionable buckets.

    Cost: ~$0.0001 per reply (Claude Haiku). A 200-reply month is ~$0.02.

    Args:
        campaign_id: The Lemlist campaign ID.
        start_date:  Inclusive start, 'YYYY-MM-DD' or ISO-8601.
        end_date:    Inclusive end, 'YYYY-MM-DD' or ISO-8601.
        limit:       Max replies to classify (newest first). Default 50, max 500.

    Returns:
        JSON with:
          - period, campaign_id, replies_found, replies_classified
          - distribution: per-category counts + percentages
          - by_reply: list of {lead_email, at, category, confidence, reason,
                               subject_preview, body_preview}
          - cost_estimate_usd
    """
    from collections import defaultdict
    try:
        start_dt = _parse_date_param(start_date, end_of_day=False)
        end_dt = _parse_date_param(end_date, end_of_day=True)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, indent=2)

    limit = max(1, min(int(limit or 50), 500))

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return json.dumps({
            "error": "ANTHROPIC_API_KEY is not set. The reply classifier needs "
                     "an Anthropic API key to call Claude Haiku."
        }, indent=2)

    acts, truncated, err = _paginate_activities(
        campaign_id, "emailsReplied", start_dt, end_dt
    )
    # Newest first.
    acts.sort(key=lambda a: a.get("_ts") or start_dt, reverse=True)
    acts = acts[:limit]

    try:
        import anthropic  # local import: keeps module import cheap if unused
    except Exception as e:
        return json.dumps({"error": f"anthropic SDK not installed: {e}"}, indent=2)
    client = anthropic.Anthropic(api_key=api_key)
    # Model id: defaults to current Haiku family alias. The Anthropic API
    # resolves '-latest' / family aliases server-side, so this stays valid
    # across new Haiku releases without code changes. Override via env var
    # LEMLIST_REPLY_CLASSIFIER_MODEL if you want a specific dated build.
    model = os.environ.get("LEMLIST_REPLY_CLASSIFIER_MODEL", "claude-haiku-4-5")

    by_reply = []
    counts = defaultdict(int)
    classified = 0
    classify_errors = 0
    for a in acts:
        subject, body = _extract_reply_text(a)
        snippet_body = (body or "")[:2000]
        snippet_subject = (subject or "")[:200]
        if not snippet_body and not snippet_subject:
            counts["other"] += 1
            by_reply.append({
                "lead_email": a.get("leadEmail") or a.get("email") or "",
                "at": (a["_ts"].isoformat() if a.get("_ts") else ""),
                "category": "other",
                "confidence": 0.0,
                "reason": "no body or subject in activity",
                "subject_preview": "",
                "body_preview": "",
            })
            continue
        user_msg = f"SUBJECT: {snippet_subject}\n\nBODY:\n{snippet_body}"
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=120,
                system=_REPLY_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = "".join(
                getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
            ).strip()
            # Strip fenced code if Claude wraps it.
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.lower().startswith("json"):
                    raw = raw[4:].strip()
            parsed = json.loads(raw)
            cat = str(parsed.get("category", "other")).lower()
            if cat not in {"positive", "negative", "ooo", "unsubscribe", "referral", "other"}:
                cat = "other"
            conf = float(parsed.get("confidence") or 0.0)
            reason = str(parsed.get("reason") or "")[:200]
            classified += 1
        except Exception as exc:
            classify_errors += 1
            cat = "other"
            conf = 0.0
            reason = f"classifier_error: {str(exc)[:120]}"

        counts[cat] += 1
        by_reply.append({
            "lead_email": a.get("leadEmail") or a.get("email") or "",
            "at": (a["_ts"].isoformat() if a.get("_ts") else ""),
            "category": cat,
            "confidence": round(conf, 2),
            "reason": reason,
            "subject_preview": snippet_subject[:120],
            "body_preview": snippet_body[:300],
        })

    total = sum(counts.values()) or 1
    distribution = {
        k: {"count": counts.get(k, 0),
            "pct": round(counts.get(k, 0) / total * 100, 1)}
        for k in ("positive", "negative", "ooo", "unsubscribe", "referral", "other")
    }
    # Rough cost: Haiku ~ $0.80/MTok in, $4/MTok out. We send ~600 tokens, get ~60.
    cost_estimate = round(classified * (600 * 0.80 + 60 * 4.0) / 1_000_000, 4)

    return json.dumps({
        "status": "partial" if (truncated or err or classify_errors) else "success",
        "period": {"start": start_date, "end": end_date},
        "campaign_id": campaign_id,
        "replies_found": len(acts),
        "replies_classified": classified,
        "classifier_errors": classify_errors,
        "distribution": distribution,
        "by_reply": by_reply,
        "cost_estimate_usd": cost_estimate,
        "model": model,
        "truncated_fetch": truncated,
        "fetch_error": err,
        "note": (
            "Replies are classified by Claude Haiku from the activity's reply "
            "body + subject. If Lemlist didn't capture the body for a reply "
            "(common for forwarded threads), the row lands in 'other' with "
            "reason='no body or subject in activity'. Cost estimate is a "
            "ballpark using public Haiku pricing — actual billing comes from "
            "your Anthropic dashboard."
        ),
    }, indent=2, default=str)


# ============================================================================
# VALIDATED PUSH GATEWAY
# ----------------------------------------------------------------------------
# Replaces direct agent use of lemlist_add_lead_to_campaign for ABM flows.
# Hard server-side predicates:
#   1. Re-query SF for every contact_sf_id and REJECT any whose AccountId
#      doesn't match the requested account_id (kills cross-account bleed).
#   2. Resolve owner_email -> usr_xxx via /team/senders (kills owner spoofing).
#   3. Pre-flight /leads/{email} for every contact; route conflicts per
#      on_conflict (kills cross-campaign bleed).
#   4. Build the Lemlist payload from SF data, NOT agent input, for identity
#      fields. Agent only supplies per-email personalization (CTA1, customBody1
#      etc.) via custom_fields_per_email.
#   5. Write one row per attempt to public.lemlist_push_receipts. Every claim
#      the agent later makes about "I pushed X" is verifiable: zero receipts =
#      zero pushes, full stop.
# ============================================================================

_GATEWAY_TIMEOUT = 30.0

import re as _re
_SF_ID_RE = _re.compile(r"^[a-zA-Z0-9]{15}([a-zA-Z0-9]{3})?$")


def _is_valid_sf_id(s) -> bool:
    """Salesforce IDs are exactly 15 or 18 alphanumeric characters. Anything
    else is rejected before it can reach SOQL interpolation."""
    return isinstance(s, str) and bool(_SF_ID_RE.match(s))


def _sf15(account_id: str) -> str:
    """Salesforce IDs come in 15-char (case-sensitive) and 18-char (case-safe)
    forms. Compare on the 15-char prefix so '001P...IHIAZ' and '001P...IH'
    match the same record."""
    if not account_id:
        return ""
    s = str(account_id).strip()
    return s[:15]


def _gateway_sf_lookup_contacts(contact_sf_ids: list) -> dict:
    """Re-query SF for the given Contact IDs. Returns {Id: row_dict}.
    Rows that don't exist are simply absent from the result map."""
    sf = _get_sf_connection()
    if sf is None:
        raise RuntimeError(
            "Salesforce connection unavailable — cannot validate contacts. "
            "Refusing to push. (Set SF_USERNAME/SF_PASSWORD/SF_SECURITY_TOKEN.)"
        )
    if not contact_sf_ids:
        return {}
    # Strict allowlist — rejected IDs never reach SOQL. Caller filtered earlier
    # but we double-check here so any future caller of this helper is safe too.
    safe_ids = [cid for cid in contact_sf_ids if _is_valid_sf_id(cid)]
    if not safe_ids:
        return {}
    quoted = ",".join("'" + cid + "'" for cid in safe_ids)
    soql = (
        "SELECT Id, AccountId, FirstName, LastName, Email, Phone, MobilePhone, "
        "LinkedIn_Profile__c, Title, Account.Name "
        f"FROM Contact WHERE Id IN ({quoted})"
    )
    res = sf.query(soql)
    out = {}
    for rec in (res.get("records") or []):
        out[rec["Id"][:15]] = rec
        out[rec["Id"]] = rec
    return out


# Process-lifetime cache: userId -> email. Avoids hitting /users/{uid} on
# every push. Invalidated only by server restart, which is acceptable since
# sender emails change rarely.
_sender_email_cache: dict = {}

# Zycus organizational alias domains — per Phase 6 doc, these are treated as
# the same identity when the local-part matches. Keep this list in sync with
# the Phase 6 spec.
_ZYCUS_ALIAS_DOMAINS = {
    "zycus.com",
    "teamzycus.com",
    "zycusoptimization.com",
    "zycusintake.com",
    "zycus-beyond.com",
    "boostzycus.com",
}


def _resolve_sender_email(user_id: str) -> Optional[str]:
    """Look up a Lemlist user's primary email from /users/{uid}, with cache.
    Returns the email string or None if the lookup fails."""
    if not user_id:
        return None
    if user_id in _sender_email_cache:
        return _sender_email_cache[user_id]
    try:
        rec = _get(f"/users/{user_id}")
    except Exception:
        return None
    email = None
    if isinstance(rec, dict):
        for k in ("email", "userEmail", "login", "username"):
            v = rec.get(k)
            if isinstance(v, str) and "@" in v:
                email = v.strip().lower()
                break
    _sender_email_cache[user_id] = email
    return email


def _gateway_resolve_owner(owner_email: str) -> Optional[dict]:
    """Resolve a BD owner email to a Lemlist team sender.

    /team/senders returns records of shape {userId, campaigns} — NO email
    field. So we have to: (1) get the approved userIds from /team/senders,
    (2) resolve each to its actual email via /users/{uid} (cached), (3)
    match the input owner_email against the resolved emails.

    Match strategy (per Phase 6 sender resolution policy):
      1. Exact case-insensitive match against the resolved email.
      2. Local-part match across approved Zycus alias domains
         (e.g. ruchi.yadav@teamzycus.com ≈ ruchi.yadav@zycus.com).

    Returns:
        dict with keys: _id (= userId, used downstream as contactOwner),
        userId, email (the resolved email from /users), campaigns
        (sender's assigned campaigns), match_type ("exact" | "alias").
        Returns None if no sender matches.
    """
    if not owner_email:
        return None
    target = owner_email.strip().lower()
    if "@" not in target:
        return None
    target_local, target_domain = target.split("@", 1)

    try:
        senders = _get("/team/senders") or []
    except Exception as e:
        raise RuntimeError(f"Failed to list /team/senders for owner resolution: {e}")
    if not isinstance(senders, list):
        return None

    # Resolve all sender emails up front (mostly cache hits after first call).
    resolved = []  # list of (user_id, email, sender_record)
    for s in senders:
        if not isinstance(s, dict):
            continue
        uid = s.get("userId")
        if not uid:
            continue
        email = _resolve_sender_email(uid)
        resolved.append((uid, email, s))

    # Pass 1: exact email match.
    for uid, email, s in resolved:
        if email and email == target:
            return {
                "_id": uid,
                "userId": uid,
                "email": email,
                "campaigns": s.get("campaigns") or [],
                "match_type": "exact",
            }

    # Pass 2: alias-domain match — same local-part, both domains in the
    # approved Zycus alias set. Collect ALL candidates so we can hard-fail
    # on ambiguity rather than silently picking the first match.
    alias_candidates = []
    if target_domain in _ZYCUS_ALIAS_DOMAINS:
        for uid, email, s in resolved:
            if not email or "@" not in email:
                continue
            local, domain = email.split("@", 1)
            if (local == target_local
                    and domain in _ZYCUS_ALIAS_DOMAINS):
                alias_candidates.append((uid, email, s))

    if len(alias_candidates) == 1:
        uid, email, s = alias_candidates[0]
        return {
            "_id": uid,
            "userId": uid,
            "email": email,
            "campaigns": s.get("campaigns") or [],
            "match_type": "alias",
            "alias_note": (
                f"Input {owner_email!r} resolved to canonical sender "
                f"{email!r} via Zycus alias-domain rule."
            ),
        }
    if len(alias_candidates) > 1:
        # Multiple senders share the local-part across alias domains.
        # Refuse rather than guess — caller must supply the canonical email.
        raise RuntimeError(
            f"owner_email {owner_email!r} matches multiple Lemlist senders "
            f"by alias-domain rule: "
            f"{[e for _, e, _ in alias_candidates]}. Push aborted — please "
            f"specify the canonical sender email explicitly."
        )
    return None


def _gateway_pre_flight(email: str, target_campaign_id: str) -> dict:
    """Check whether `email` is already enrolled in a different campaign.
    Returns one of three states:
      - {'state': 'clear'}                              — safe to push
      - {'state': 'conflict', 'other_campaign_id': ...} — in another campaign
      - {'state': 'unknown', 'error': '...'}            — pre-flight failed;
        caller MUST treat this as fail-closed (do not push).
    404 means the lead doesn't exist anywhere — that's 'clear'."""
    try:
        rec = _get(f"/leads/{email}")
    except RuntimeError as e:
        msg = str(e)
        if "404" in msg:
            return {"state": "clear"}
        return {"state": "unknown", "error": msg[:300]}
    except Exception as e:
        return {"state": "unknown", "error": str(e)[:300]}
    if not isinstance(rec, dict) or not rec:
        return {"state": "clear"}
    cid = rec.get("campaignId")
    if isinstance(cid, str) and cid and cid != target_campaign_id:
        return {"state": "conflict", "other_campaign_id": cid}
    camps = rec.get("campaigns")
    if isinstance(camps, list):
        for c in camps:
            if isinstance(c, dict):
                ccid = c.get("campaignId") or c.get("_id") or c.get("id")
                if ccid and ccid != target_campaign_id:
                    return {"state": "conflict", "other_campaign_id": ccid}
    return {"state": "clear"}


def _gateway_write_receipt(row: dict) -> None:
    """Insert one receipt row into public.lemlist_push_receipts via PostgREST.
    Best-effort: a write failure is logged but does NOT block the push result."""
    url = os.environ.get("SUPABASE_URL", "")
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
           or os.environ.get("SUPABASE_SERVICE_KEY") or "")
    if not url or not key:
        _sf_logger.warning("[push_gateway] no Supabase creds, skipping receipt write")
        return
    try:
        r = httpx.post(
            f"{url.rstrip('/')}/rest/v1/lemlist_push_receipts",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json=row,
            timeout=4.0,
        )
        if r.status_code >= 400:
            _sf_logger.warning(
                "[push_gateway] receipt write failed HTTP %s: %s",
                r.status_code, r.text[:200],
            )
    except Exception as e:
        _sf_logger.warning("[push_gateway] receipt write raised: %s", e)


def _gateway_build_payload(sf_row: dict, owner_user_id: str,
                           custom_fields: dict) -> dict:
    """Construct the Lemlist payload from authoritative SF data. Identity
    fields (email, names, phone, linkedin, company) come from SF. The agent's
    custom_fields only contribute personalization variables (CTA1, customBody1,
    etc.) — they cannot overwrite identity."""
    email = (sf_row.get("Email") or "").strip()
    if not email:
        raise ValueError("SF contact has no Email — cannot push")
    payload: dict = {"email": email}
    fn = sf_row.get("FirstName") or ""
    ln = sf_row.get("LastName") or ""
    if fn:
        payload["firstName"] = _clean_first_name(fn, ln)
    if ln:
        payload["lastName"] = ln
    phone = sf_row.get("MobilePhone") or sf_row.get("Phone")
    if phone:
        payload["phone"] = phone
    linkedin = sf_row.get("LinkedIn_Profile__c")
    if linkedin:
        payload["linkedinUrl"] = linkedin
    title = sf_row.get("Title")
    if title:
        payload["jobTitle"] = title
    acct = sf_row.get("Account") or {}
    if isinstance(acct, dict) and acct.get("Name"):
        payload["companyName"] = acct["Name"]
    if owner_user_id:
        payload["contactOwner"] = owner_user_id

    # Personalization variables only — identity keys are stripped.
    if custom_fields:
        ZONE1 = {"firstName", "first_name", "lastName", "last_name", "email",
                 "contactOwner", "contact_owner", "phone", "linkedinUrl",
                 "linkedin_url", "companyName", "company_name", "jobTitle",
                 "job_title"}
        for k, v in custom_fields.items():
            if k in ZONE1:
                continue
            if v is None or (isinstance(v, str) and v.strip() == ""):
                continue
            payload[k] = v
    return payload


@mcp.tool()
def lemlist_validated_push(
    chat_id: str,
    account_id: str,
    campaign_id: str,
    owner_email: str,
    contact_sf_ids: List[str],
    custom_fields_per_email: Optional[dict] = None,
    on_conflict: str = "skip",
    allow_empty_custom_fields: bool = False,
) -> str:
    """
    Push SF contacts to a Lemlist campaign with hard server-side validation.
    Required for ABM (use this, NEVER lemlist_add_lead_to_campaign).

    custom_fields_per_email keys MUST be the contact's email; values are dicts
    of personalization vars (customSubject1, customBody1, customBridge1,
    customValue1, CTA1, linkedInMessage, etc.). Example:
      {"alice@co.com": {"customSubject1": "...", "customBody1": "...",
                        "CTA1": "...", "linkedInMessage": "..."}}
    Identity fields (email, firstName, lastName, phone, linkedinUrl, jobTitle,
    companyName, contactOwner) come from Salesforce and are stripped if
    included here.

    By default this tool FAILS CLOSED for any contact whose entry in
    custom_fields_per_email is missing or empty — pushing a bare lead lets
    Lemlist send sequence emails with unfilled placeholders. Set
    allow_empty_custom_fields=True ONLY for non-personalized campaigns.

    What this tool does, server-side, with no LLM in the loop:
      1. Re-queries Salesforce for every contact_sf_id you list and REJECTS
         any contact whose AccountId does not match account_id (kills cross-
         account bleed: if you accidentally pass a contact from another
         account, it is dropped, not pushed).
      2. Resolves owner_email -> Lemlist usr_xxx via /team/senders. If the
         email isn't a real Lemlist team sender, the whole push is aborted.
      3. Pre-flights every email against Lemlist; if a lead is already in a
         different campaign, the on_conflict policy decides what happens.
      4. Builds the Lemlist payload from SF data (NOT from your input) for
         identity fields. You only supply per-email personalization
         (CTA1, customBody1, customSubject1, etc.) via custom_fields_per_email.
      5. Writes one receipt row per attempt to public.lemlist_push_receipts.
         Every claim you later make about a push is verifiable by querying
         that table for this chat_id. Zero receipts means zero pushes.

    Args:
        chat_id: The DeepAgent chat UUID. Used to tag receipts.
        account_id: Salesforce Account ID (15 or 18 char). Every contact must
            belong to this account or it will be rejected.
        campaign_id: Lemlist campaign ID (e.g. "cam_abc123").
        owner_email: Zycus email of the BD owner. Must exist as a Lemlist
            team sender.
        contact_sf_ids: List of Salesforce Contact IDs (15 or 18 char). The
            server will SELECT them from SF — do not pass inline payloads.
        custom_fields_per_email: dict of {email: {var_name: value, ...}}.
            Identity keys (firstName, email, contactOwner, etc.) are stripped
            even if present here — they always come from SF.
        on_conflict: How to handle leads already enrolled in another campaign:
            "skip"  (default) — record skipped_conflict receipt, do not push.
            "abort" — stop the whole push as soon as the first conflict is
                      seen; remaining contacts are recorded as rejected.
            "move"  — currently DISABLED. A DELETE-then-add is not atomic and
                      can lose the lead if the add fails. Receipts are written
                      with action='rejected_move_unsupported'. To move a lead,
                      remove it from the old campaign manually first, then
                      call this tool with on_conflict='skip'.
            If pre-flight cannot determine state (network/API error), the
            contact is rejected with action='rejected_preflight_unknown' —
            we fail closed rather than risk a wrong-campaign push.

    Returns:
        JSON string with {pushed[], skipped_conflict[], rejected[], aborted,
        owner_user_id, summary{counts}}. Each entry has the receipt_id that
        was written to Supabase.
    """
    if on_conflict not in ("skip", "move", "abort"):
        return json.dumps({"error": f"on_conflict must be skip|move|abort, got {on_conflict!r}"})
    if not contact_sf_ids or not isinstance(contact_sf_ids, list):
        return json.dumps({"error": "contact_sf_ids must be a non-empty list of SF Contact IDs"})
    if not account_id or not campaign_id or not owner_email:
        return json.dumps({"error": "account_id, campaign_id, and owner_email are all required"})

    custom_fields_per_email = custom_fields_per_email or {}
    expected_acct_15 = _sf15(account_id)

    # [1] SF re-query
    try:
        sf_map = _gateway_sf_lookup_contacts(contact_sf_ids)
    except Exception as e:
        return json.dumps({"error": f"Salesforce lookup failed: {e}"})

    # [2] Owner resolution (now resolves email via /users/{uid} per sender,
    # matches against /team/senders userId list; supports Zycus alias domains).
    try:
        owner = _gateway_resolve_owner(owner_email)
    except RuntimeError as e:
        return json.dumps({"error": str(e)})
    if owner is None:
        return json.dumps({
            "error": (f"owner_email {owner_email!r} does not match any Lemlist "
                      f"team sender. The validator checked /team/senders and "
                      f"resolved each sender's email via /users/{{uid}}; no "
                      f"sender resolved to this email (exact or Zycus alias). "
                      f"Push aborted."),
            "hint": (
                "Call lemlist_get_team / inspect /team/senders to see approved "
                "senders. If the BD is on the team but not in /team/senders, "
                "they need to be added as a sender in Lemlist UI before any "
                "validated push can run under their ownership."
            ),
        })
    owner_user_id = owner.get("_id") or owner.get("userId") or ""
    if not owner_user_id:
        return json.dumps({
            "error": (f"Resolved sender for {owner_email!r} has no userId; "
                      f"cannot safely set contactOwner. Push aborted."),
            "sender_record": owner,
        })

    # [3] Campaign existence sanity check
    try:
        camp = _get(f"/campaigns/{campaign_id}")
        if not isinstance(camp, dict) or not camp.get("_id"):
            return json.dumps({
                "error": f"campaign_id {campaign_id!r} not found in Lemlist.",
            })
    except Exception as e:
        return json.dumps({"error": f"Campaign lookup failed: {e}"})

    # [3b] Campaign ownership check — the resolved sender must actually be
    # assigned to this campaign in /team/senders. Catches the case where the
    # BD is a valid sender on the team but not the owner of THIS campaign,
    # which would otherwise produce a successful push to the wrong owner.
    # Defensive: Lemlist returns campaign refs as `_id` today, but accept
    # `campaignId` and `id` as fallbacks in case the API evolves.
    owner_campaign_ids = {
        (c.get("_id") or c.get("campaignId") or c.get("id") or "")
        for c in (owner.get("campaigns") or [])
        if isinstance(c, dict)
    }
    owner_campaign_ids.discard("")
    if campaign_id not in owner_campaign_ids:
        return json.dumps({
            "error": (
                f"Sender {owner.get('email') or owner_email!r} "
                f"(userId={owner_user_id}) is on the Lemlist team but is NOT "
                f"assigned to campaign {campaign_id!r}. Push aborted to "
                f"prevent wrong-owner enrolment."
            ),
            "sender_assigned_campaign_count": len(owner_campaign_ids),
            "hint": (
                "Either (a) pick a different owner_email whose sender record "
                "includes this campaign, or (b) add the sender to the "
                "campaign in Lemlist UI before retrying."
            ),
        })

    pushed, skipped_conflict, rejected = [], [], []
    aborted = False

    for cid in contact_sf_ids:
        sf_row = sf_map.get(cid) or sf_map.get(_sf15(cid))

        # Reject: contact not found
        if not sf_row:
            row = {
                "chat_id": chat_id, "account_id": account_id,
                "campaign_id": campaign_id, "owner_email": owner_email,
                "owner_user_id": owner_user_id,
                "sf_contact_id": cid, "sf_account_id": "",
                "email": "", "action": "rejected_not_found",
                "error": "Contact not found in Salesforce", "payload": None,
            }
            _gateway_write_receipt(row)
            rejected.append({"sf_contact_id": cid, "reason": "not_found_in_sf"})
            continue

        contact_acct_15 = _sf15(sf_row.get("AccountId") or "")
        # Reject: cross-account bleed
        if contact_acct_15 != expected_acct_15:
            row = {
                "chat_id": chat_id, "account_id": account_id,
                "campaign_id": campaign_id, "owner_email": owner_email,
                "owner_user_id": owner_user_id,
                "sf_contact_id": sf_row["Id"],
                "sf_account_id": sf_row.get("AccountId") or "",
                "email": sf_row.get("Email") or "",
                "action": "rejected_wrong_account",
                "error": (f"Contact belongs to AccountId={contact_acct_15} "
                          f"but push targets AccountId={expected_acct_15}"),
                "payload": None,
            }
            _gateway_write_receipt(row)
            rejected.append({
                "sf_contact_id": sf_row["Id"],
                "email": sf_row.get("Email"),
                "reason": "wrong_account",
                "contact_account_id": sf_row.get("AccountId"),
                "expected_account_id": account_id,
            })
            continue

        email = (sf_row.get("Email") or "").strip()
        if not email:
            row = {
                "chat_id": chat_id, "account_id": account_id,
                "campaign_id": campaign_id, "owner_email": owner_email,
                "owner_user_id": owner_user_id,
                "sf_contact_id": sf_row["Id"],
                "sf_account_id": sf_row.get("AccountId") or "",
                "email": "", "action": "rejected_no_email",
                "error": "SF contact has no Email", "payload": None,
            }
            _gateway_write_receipt(row)
            rejected.append({"sf_contact_id": sf_row["Id"], "reason": "no_email"})
            continue

        # [3d] Fail-closed: ABM pushes must include custom variables.
        # Pushing a lead without custom_fields_per_email[email] populated
        # produces a Lemlist lead with no personalization data, so any
        # sequence step referencing {{customSubject1}}, {{customBody1}},
        # {{CTA1}}, etc. will go out as empty/placeholder text. Refuse the
        # push unless allow_empty_custom_fields=True (non-personalized
        # campaigns only).
        if not allow_empty_custom_fields:
            cv = custom_fields_per_email.get(email) or {}
            if not cv:
                row = {
                    "chat_id": chat_id, "account_id": account_id,
                    "campaign_id": campaign_id, "owner_email": owner_email,
                    "owner_user_id": owner_user_id,
                    "sf_contact_id": sf_row["Id"],
                    "sf_account_id": sf_row.get("AccountId") or "",
                    "email": email, "action": "rejected_no_custom_vars",
                    "error": (
                        "No custom variables passed for this email — would "
                        "land in Lemlist as a bare lead and the sequence "
                        "would send emails with empty placeholders. Pass "
                        f"custom_fields_per_email[{email!r}] with "
                        "customSubject1/customBody1/CTA1/linkedInMessage/"
                        "etc., or set allow_empty_custom_fields=True if "
                        "this campaign genuinely has no personalization."
                    ),
                    "payload": None,
                }
                _gateway_write_receipt(row)
                rejected.append({
                    "email": email, "reason": "no_custom_vars",
                    "hint": (
                        f"add custom_fields_per_email[{email!r}] with "
                        "personalization vars, or pass "
                        "allow_empty_custom_fields=True"
                    ),
                })
                continue

        # [4] Pre-flight conflict check (fail-closed on unknown)
        pf = _gateway_pre_flight(email, campaign_id)
        pf_state = pf.get("state", "unknown")

        if pf_state == "unknown":
            # Pre-flight could not determine state. Refuse to push rather than
            # silently risk a wrong-campaign outcome.
            row = {
                "chat_id": chat_id, "account_id": account_id,
                "campaign_id": campaign_id, "owner_email": owner_email,
                "owner_user_id": owner_user_id,
                "sf_contact_id": sf_row["Id"],
                "sf_account_id": sf_row.get("AccountId") or "",
                "email": email, "action": "rejected_preflight_unknown",
                "error": f"Pre-flight failed (fail-closed): {pf.get('error', 'unknown')}",
                "payload": None,
            }
            _gateway_write_receipt(row)
            rejected.append({
                "email": email, "reason": "preflight_unknown",
                "detail": pf.get("error"),
            })
            continue

        if pf_state == "conflict":
            other_cid = pf.get("other_campaign_id")
            if on_conflict == "abort":
                aborted = True
                row = {
                    "chat_id": chat_id, "account_id": account_id,
                    "campaign_id": campaign_id, "owner_email": owner_email,
                    "owner_user_id": owner_user_id,
                    "sf_contact_id": sf_row["Id"],
                    "sf_account_id": sf_row.get("AccountId") or "",
                    "email": email, "action": "rejected_conflict_abort",
                    "error": f"Lead already in campaign {other_cid}; on_conflict=abort",
                    "payload": None,
                }
                _gateway_write_receipt(row)
                rejected.append({
                    "email": email, "reason": "conflict_abort",
                    "other_campaign_id": other_cid,
                })
                break  # stop processing further contacts
            elif on_conflict == "skip":
                row = {
                    "chat_id": chat_id, "account_id": account_id,
                    "campaign_id": campaign_id, "owner_email": owner_email,
                    "owner_user_id": owner_user_id,
                    "sf_contact_id": sf_row["Id"],
                    "sf_account_id": sf_row.get("AccountId") or "",
                    "email": email, "action": "skipped_conflict",
                    "error": f"Lead already in campaign {other_cid}",
                    "payload": None,
                }
                _gateway_write_receipt(row)
                skipped_conflict.append({
                    "email": email, "other_campaign_id": other_cid,
                })
                continue
            elif on_conflict == "move":
                # 'move' is currently UNSUPPORTED: a DELETE-then-add is not
                # atomic and can lose the lead if the subsequent add fails.
                # Until a safe two-phase move is implemented, treat it as a
                # rejection and tell the caller to handle the move manually
                # (remove the lead from the old campaign yourself, then call
                # this tool again with on_conflict='skip').
                row = {
                    "chat_id": chat_id, "account_id": account_id,
                    "campaign_id": campaign_id, "owner_email": owner_email,
                    "owner_user_id": owner_user_id,
                    "sf_contact_id": sf_row["Id"],
                    "sf_account_id": sf_row.get("AccountId") or "",
                    "email": email, "action": "rejected_move_unsupported",
                    "error": (f"Lead already in campaign {other_cid}; "
                              f"on_conflict='move' is currently disabled "
                              f"because a DELETE-then-add is not atomic. "
                              f"Remove the lead from {other_cid} manually, "
                              f"then re-run with on_conflict='skip'."),
                    "payload": None,
                }
                _gateway_write_receipt(row)
                rejected.append({
                    "email": email, "reason": "move_unsupported",
                    "other_campaign_id": other_cid,
                })
                continue

        # [5] Build payload from SF + push
        try:
            payload = _gateway_build_payload(
                sf_row, owner_user_id,
                custom_fields_per_email.get(email) or {},
            )
        except Exception as e:
            row = {
                "chat_id": chat_id, "account_id": account_id,
                "campaign_id": campaign_id, "owner_email": owner_email,
                "owner_user_id": owner_user_id,
                "sf_contact_id": sf_row["Id"],
                "sf_account_id": sf_row.get("AccountId") or "",
                "email": email, "action": "rejected_payload_error",
                "error": str(e), "payload": None,
            }
            _gateway_write_receipt(row)
            rejected.append({"email": email, "reason": f"payload_error: {e}"})
            continue

        api_path = f"/campaigns/{campaign_id}/leads?deduplicate=true"
        try:
            result = _post(api_path, payload)
            lead_id = result.get("_id") if isinstance(result, dict) else None
            added_date = datetime.now(_tz_utc_module.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
            try:
                _sync_campaign_date_to_salesforce(email, added_date)
            except Exception:
                pass
            row = {
                "chat_id": chat_id, "account_id": account_id,
                "campaign_id": campaign_id, "owner_email": owner_email,
                "owner_user_id": owner_user_id,
                "sf_contact_id": sf_row["Id"],
                "sf_account_id": sf_row.get("AccountId") or "",
                "email": email, "action": "pushed",
                "http_status": 200,
                "lemlist_lead_id": lead_id,
                "api_method": "POST",
                "api_endpoint": f"{LEMLIST_API_BASE_URL}{api_path}" if 'LEMLIST_API_BASE_URL' in globals() else f"https://api.lemlist.com/api{api_path}",
                "payload": payload,
            }
            _gateway_write_receipt(row)
            pushed.append({
                "sf_contact_id": sf_row["Id"], "email": email,
                "lemlist_lead_id": lead_id,
            })
        except Exception as e:
            err_text = str(e)
            http_status = None
            m = _re.search(r"HTTP\s+(\d{3})", err_text)
            if m:
                http_status = int(m.group(1))
            row = {
                "chat_id": chat_id, "account_id": account_id,
                "campaign_id": campaign_id, "owner_email": owner_email,
                "owner_user_id": owner_user_id,
                "sf_contact_id": sf_row["Id"],
                "sf_account_id": sf_row.get("AccountId") or "",
                "email": email, "action": "push_failed",
                "http_status": http_status,
                "error": err_text[:500],
                "api_method": "POST",
                "api_endpoint": f"{LEMLIST_API_BASE_URL}{api_path}" if 'LEMLIST_API_BASE_URL' in globals() else f"https://api.lemlist.com/api{api_path}",
                "payload": payload,
            }
            _gateway_write_receipt(row)
            rejected.append({"email": email, "reason": f"push_failed: {e}"})

    return json.dumps({
        "chat_id": chat_id,
        "account_id": account_id,
        "campaign_id": campaign_id,
        "owner_email": owner_email,
        "owner_user_id": owner_user_id,
        "aborted": aborted,
        "pushed": pushed,
        "skipped_conflict": skipped_conflict,
        "rejected": rejected,
        "summary": {
            "requested": len(contact_sf_ids),
            "pushed": len(pushed),
            "skipped_conflict": len(skipped_conflict),
            "rejected": len(rejected),
        },
        "verification_note": (
            "Every entry above corresponds to a row in "
            "public.lemlist_push_receipts (filter by chat_id). Zero rows "
            "with action='pushed' means zero leads were actually added to "
            "the campaign — do not claim otherwise."
        ),
    }, indent=2, default=str)


@mcp.tool()
def lemlist_get_push_receipts(chat_id: str, campaign_id: Optional[str] = None) -> str:
    """
    Read back the audit trail of validated pushes for a chat. Use this when
    asked 'did you actually push?' or 'what got pushed in this chat?'. This
    is the only authoritative source of truth — if it returns zero rows with
    action='pushed', no leads were added regardless of what any prior
    assistant message claimed.

    Args:
        chat_id: The DeepAgent chat UUID.
        campaign_id: Optional Lemlist campaign filter.

    Returns:
        JSON string with the list of receipts and per-action counts.
    """
    url = os.environ.get("SUPABASE_URL", "")
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
           or os.environ.get("SUPABASE_SERVICE_KEY") or "")
    if not url or not key:
        return json.dumps({"error": "Supabase credentials not configured"})
    params = {"chat_id": f"eq.{chat_id}", "order": "created_at.asc"}
    if campaign_id:
        params["campaign_id"] = f"eq.{campaign_id}"
    try:
        r = httpx.get(
            f"{url.rstrip('/')}/rest/v1/lemlist_push_receipts",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            params=params, timeout=15.0,
        )
        if r.status_code >= 400:
            return json.dumps({"error": f"HTTP {r.status_code}", "detail": r.text[:300]})
        rows = r.json() or []
    except Exception as e:
        return json.dumps({"error": f"receipt read failed: {e}"})
    counts: dict = {}
    for row in rows:
        a = row.get("action") or "unknown"
        counts[a] = counts.get(a, 0) + 1
    return json.dumps({
        "chat_id": chat_id,
        "campaign_id": campaign_id,
        "total_receipts": len(rows),
        "counts_by_action": counts,
        "receipts": rows,
    }, indent=2, default=str)


if __name__ == "__main__":
    mcp.run()
