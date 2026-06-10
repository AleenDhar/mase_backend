import os
import base64
import json
from typing import Optional

import httpx
from fastmcp import FastMCP

SITE_NAME = os.environ.get("ELOQUA_SITE_NAME", "").strip()
USERNAME = os.environ.get("ELOQUA_USERNAME", "").strip()
PASSWORD = os.environ.get("ELOQUA_PASSWORD", "").strip()

_BASE_URL: Optional[str] = None
_REST_BASE: Optional[str] = None   # e.g. https://secure.p06.eloqua.com/api/rest/2.0

mcp = FastMCP(
    name="Eloqua",
    instructions=(
        "Use this server to manage Oracle Eloqua contacts, campaigns, email assets, "
        "contact lists, and engagement/activity data. "
        "Requires ELOQUA_SITE_NAME, ELOQUA_USERNAME, and ELOQUA_PASSWORD env vars. "
        "Eloqua is Oracle's B2B marketing automation platform used for campaign management, "
        "lead nurturing, email marketing, and contact engagement tracking."
    ),
)


def _auth_header() -> str:
    if not (SITE_NAME and USERNAME and PASSWORD):
        raise RuntimeError(
            "Eloqua credentials missing. Set ELOQUA_SITE_NAME, ELOQUA_USERNAME, "
            "and ELOQUA_PASSWORD environment variables."
        )
    compound = f"{SITE_NAME}\\{USERNAME}:{PASSWORD}"
    encoded = base64.b64encode(compound.encode()).decode()
    return f"Basic {encoded}"


def _headers() -> dict:
    return {
        "Authorization": _auth_header(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _resolve_base_url() -> str:
    """Authenticate with login.eloqua.com/id and cache the REST API base URL.

    The /id endpoint returns the full URL template for the REST API, e.g.
    https://secure.p06.eloqua.com/api/rest/{version}/
    We substitute {version} with 2.0 and strip the trailing slash.
    """
    global _BASE_URL, _REST_BASE
    if _REST_BASE:
        return _REST_BASE
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(
                "https://login.eloqua.com/id",
                headers={"Authorization": _auth_header(), "Accept": "application/json"},
            )
        if resp.status_code == 401:
            raise RuntimeError(
                "Eloqua authentication failed. Check ELOQUA_SITE_NAME, "
                "ELOQUA_USERNAME, and ELOQUA_PASSWORD."
            )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, str):
            raise RuntimeError(f"Eloqua auth rejected: {data}")

        _BASE_URL = data.get("urls", {}).get("base", "")

        rest_template = (
            data.get("urls", {})
                .get("apis", {})
                .get("rest", {})
                .get("standard", "")
        )
        if rest_template:
            _REST_BASE = rest_template.replace("{version}", "2.0").rstrip("/")
        elif _BASE_URL:
            _REST_BASE = f"{_BASE_URL}/api/rest/2.0"
        else:
            raise RuntimeError(f"Could not determine Eloqua REST URL. Response: {data}")

        return _REST_BASE
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Failed to resolve Eloqua base URL: {exc}") from exc


def _api_url(endpoint: str) -> str:
    rest_base = _resolve_base_url()
    return f"{rest_base}/{endpoint.lstrip('/')}"


def _handle_error(resp: httpx.Response, context: str) -> str:
    code = resp.status_code
    if code == 400:
        return json.dumps({"error": f"Bad request ({context})", "detail": resp.text[:500]})
    if code == 401:
        global _BASE_URL, _REST_BASE
        _BASE_URL = None
        _REST_BASE = None
        return json.dumps({"error": "Authentication failed. Credentials may be invalid."})
    if code == 403:
        return json.dumps({"error": f"Access denied ({context}). Check user permissions."})
    if code == 404:
        return json.dumps({"error": f"Not found ({context})."})
    if code == 429:
        return json.dumps({"error": "Rate limit reached. Retry after a short wait."})
    return json.dumps({"error": f"Eloqua API error {code} ({context})", "detail": resp.text[:500]})


def _get(endpoint: str, params: dict = None) -> dict | list | str:
    url = _api_url(endpoint)
    with httpx.Client(timeout=60) as client:
        resp = client.get(url, headers=_headers(), params=params or {})
    if resp.status_code >= 400:
        return json.loads(_handle_error(resp, f"GET {endpoint}"))
    try:
        return resp.json()
    except Exception:
        return resp.text


def _post(endpoint: str, payload: dict) -> dict | str:
    url = _api_url(endpoint)
    with httpx.Client(timeout=60) as client:
        resp = client.post(url, headers=_headers(), json=payload)
    if resp.status_code >= 400:
        return json.loads(_handle_error(resp, f"POST {endpoint}"))
    try:
        return resp.json()
    except Exception:
        return resp.text


def _put(endpoint: str, payload: dict) -> dict | str:
    url = _api_url(endpoint)
    with httpx.Client(timeout=60) as client:
        resp = client.put(url, headers=_headers(), json=payload)
    if resp.status_code >= 400:
        return json.loads(_handle_error(resp, f"PUT {endpoint}"))
    try:
        return resp.json()
    except Exception:
        return resp.text


def _delete_req(endpoint: str, payload: dict = None) -> dict | str:
    url = _api_url(endpoint)
    with httpx.Client(timeout=60) as client:
        resp = client.request("DELETE", url, headers=_headers(), json=payload or {})
    if resp.status_code >= 400:
        return json.loads(_handle_error(resp, f"DELETE {endpoint}"))
    if resp.status_code == 204 or not resp.content:
        return {"success": True}
    try:
        return resp.json()
    except Exception:
        return resp.text


# ---------------------------------------------------------------------------
# Contact Management
# ---------------------------------------------------------------------------

@mcp.tool()
def eloqua_search_contacts(
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    company: str = "",
    page: int = 1,
    page_size: int = 20,
) -> str:
    """
    Search Eloqua contacts by email, name, or company.

    Args:
        email: Filter by email address (partial match supported).
        first_name: Filter by first name (partial match).
        last_name: Filter by last name (partial match).
        company: Filter by company/account name.
        page: Page number (1-based, default 1).
        page_size: Results per page (default 20, max 100).

    Returns:
        JSON with list of matching contacts and total count.
    """
    search_parts = []
    if email:
        search_parts.append(f"emailAddress='{email}'")
    if first_name:
        search_parts.append(f"firstName='{first_name}'")
    if last_name:
        search_parts.append(f"lastName='{last_name}'")
    if company:
        search_parts.append(f"accountName='{company}'")

    params = {
        "count": min(page_size, 100),
        "page": page,
        "depth": "partial",
    }
    if search_parts:
        params["search"] = " AND ".join(search_parts)

    result = _get("data/contacts", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def eloqua_get_contact(
    contact_id: str = "",
    email: str = "",
) -> str:
    """
    Get a single Eloqua contact by ID or email address.

    Args:
        contact_id: The Eloqua contact ID (numeric string).
        email: Email address to look up (used if contact_id not provided).

    Returns:
        JSON with full contact record including all field values.
    """
    if contact_id:
        result = _get(f"data/contact/{contact_id}", {"depth": "complete"})
    elif email:
        result = _get("data/contacts", {"search": f"emailAddress='{email}'", "depth": "complete", "count": 1})
        if isinstance(result, dict) and result.get("elements"):
            result = result["elements"][0]
    else:
        return json.dumps({"error": "Provide contact_id or email."})
    return json.dumps(result, indent=2)


@mcp.tool()
def eloqua_create_contact(
    email: str,
    first_name: str = "",
    last_name: str = "",
    company: str = "",
    title: str = "",
    phone: str = "",
    extra_fields: str = "",
) -> str:
    """
    Create a new contact in Eloqua.

    Args:
        email: Contact's email address (required).
        first_name: First name.
        last_name: Last name.
        company: Company/account name.
        title: Job title.
        phone: Business phone number.
        extra_fields: JSON string of additional field name/value pairs,
                      e.g. '{"city": "Mumbai", "country": "India"}'.

    Returns:
        JSON with the newly created contact record including Eloqua ID.
    """
    payload: dict = {"emailAddress": email}
    if first_name:
        payload["firstName"] = first_name
    if last_name:
        payload["lastName"] = last_name
    if company:
        payload["accountName"] = company
    if title:
        payload["title"] = title
    if phone:
        payload["businessPhone"] = phone

    if extra_fields:
        try:
            extras = json.loads(extra_fields)
            field_values = []
            for k, v in extras.items():
                field_values.append({"type": "FieldValue", "id": k, "value": str(v)})
            if field_values:
                payload["fieldValues"] = field_values
        except json.JSONDecodeError:
            return json.dumps({"error": "extra_fields must be valid JSON."})

    result = _post("data/contacts", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def eloqua_update_contact(
    contact_id: str,
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    company: str = "",
    title: str = "",
    phone: str = "",
    extra_fields: str = "",
) -> str:
    """
    Update fields on an existing Eloqua contact.

    Args:
        contact_id: The Eloqua contact ID (required).
        email: New email address.
        first_name: New first name.
        last_name: New last name.
        company: New company/account name.
        title: New job title.
        phone: New business phone.
        extra_fields: JSON string of additional field name/value pairs to update.

    Returns:
        JSON with the updated contact record.
    """
    payload: dict = {"id": contact_id}
    if email:
        payload["emailAddress"] = email
    if first_name:
        payload["firstName"] = first_name
    if last_name:
        payload["lastName"] = last_name
    if company:
        payload["accountName"] = company
    if title:
        payload["title"] = title
    if phone:
        payload["businessPhone"] = phone

    if extra_fields:
        try:
            extras = json.loads(extra_fields)
            field_values = []
            for k, v in extras.items():
                field_values.append({"type": "FieldValue", "id": k, "value": str(v)})
            if field_values:
                payload["fieldValues"] = field_values
        except json.JSONDecodeError:
            return json.dumps({"error": "extra_fields must be valid JSON."})

    result = _put(f"data/contact/{contact_id}", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def eloqua_get_contact_fields() -> str:
    """
    List all available contact field definitions in Eloqua.

    Returns:
        JSON array of field definitions including internal names, display names,
        data types, and field IDs — useful when mapping custom fields.
    """
    result = _get("assets/contact/fields", {"count": 200, "depth": "complete"})
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Campaign Management
# ---------------------------------------------------------------------------

@mcp.tool()
def eloqua_list_campaigns(
    status: str = "",
    page: int = 1,
    page_size: int = 20,
) -> str:
    """
    List Eloqua campaigns with an optional status filter.

    Args:
        status: Filter by campaign status — 'active', 'draft', 'complete', or '' for all.
        page: Page number (default 1).
        page_size: Results per page (default 20, max 100).

    Returns:
        JSON with campaign list including name, ID, status, start/end dates.
    """
    params: dict = {
        "count": min(page_size, 100),
        "page": page,
        "depth": "partial",
    }
    if status:
        params["search"] = f"currentStatus='{status}'"

    result = _get("assets/campaigns", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def eloqua_get_campaign(campaign_id: str) -> str:
    """
    Get full details for a single Eloqua campaign.

    Args:
        campaign_id: The Eloqua campaign ID.

    Returns:
        JSON with campaign configuration, segments, start/end dates,
        email assets, and current status.
    """
    result = _get(f"assets/campaign/{campaign_id}", {"depth": "complete"})
    return json.dumps(result, indent=2)


@mcp.tool()
def eloqua_activate_campaign(campaign_id: str) -> str:
    """
    Activate (launch) a draft campaign in Eloqua.

    Args:
        campaign_id: The Eloqua campaign ID to activate.

    Returns:
        JSON confirming the campaign has been activated, or an error message.
    """
    result = _put(f"assets/campaign/{campaign_id}", {"id": campaign_id, "currentStatus": "Active"})
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Email Assets
# ---------------------------------------------------------------------------

@mcp.tool()
def eloqua_list_emails(
    search: str = "",
    page: int = 1,
    page_size: int = 20,
) -> str:
    """
    List Eloqua email templates and assets.

    Args:
        search: Optional name/subject keyword search.
        page: Page number (default 1).
        page_size: Results per page (default 20, max 100).

    Returns:
        JSON with email assets including name, ID, subject line, and folder path.
    """
    params: dict = {
        "count": min(page_size, 100),
        "page": page,
        "depth": "partial",
    }
    if search:
        params["search"] = f"name='{search}'"

    result = _get("assets/emails", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def eloqua_get_email(email_id: str) -> str:
    """
    Get full details for an Eloqua email asset, including HTML body and send stats.

    Args:
        email_id: The Eloqua email asset ID.

    Returns:
        JSON with email body, subject, sender info, reply-to, encoding,
        and associated campaign/folder details.
    """
    result = _get(f"assets/email/{email_id}", {"depth": "complete"})
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Contact Lists
# ---------------------------------------------------------------------------

@mcp.tool()
def eloqua_list_contact_lists(
    search: str = "",
    page: int = 1,
    page_size: int = 20,
) -> str:
    """
    List all static contact lists in Eloqua.

    Args:
        search: Optional name keyword to filter lists.
        page: Page number (default 1).
        page_size: Results per page (default 20, max 100).

    Returns:
        JSON with contact list names, IDs, member counts, and creation dates.
    """
    params: dict = {
        "count": min(page_size, 100),
        "page": page,
        "depth": "partial",
    }
    if search:
        params["search"] = f"name='{search}'"

    result = _get("assets/contact/lists", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def eloqua_get_list_members(
    list_id: str,
    page: int = 1,
    page_size: int = 50,
) -> str:
    """
    Get contacts belonging to an Eloqua static contact list.

    Args:
        list_id: The Eloqua contact list ID.
        page: Page number (default 1).
        page_size: Results per page (default 50, max 100).

    Returns:
        JSON array of contact records in the list with email, name, and ID.
    """
    params = {
        "count": min(page_size, 100),
        "page": page,
        "depth": "partial",
        "listId": list_id,
    }
    result = _get("data/contacts", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def eloqua_add_contacts_to_list(
    list_id: str,
    contact_ids: str,
) -> str:
    """
    Add one or more contacts to an Eloqua static contact list.

    Args:
        list_id: The Eloqua contact list ID.
        contact_ids: Comma-separated list of Eloqua contact IDs to add,
                     e.g. "12345,67890,11223".

    Returns:
        JSON confirming the contacts were added, or an error message.
    """
    ids = [cid.strip() for cid in contact_ids.split(",") if cid.strip()]
    if not ids:
        return json.dumps({"error": "Provide at least one contact_id."})

    payload = {
        "type": "ContactList",
        "id": list_id,
        "membershipAdditions": [{"type": "Contact", "id": cid} for cid in ids],
    }
    result = _put(f"assets/contact/list/{list_id}", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def eloqua_remove_from_list(
    list_id: str,
    contact_ids: str,
) -> str:
    """
    Remove one or more contacts from an Eloqua static contact list.

    Args:
        list_id: The Eloqua contact list ID.
        contact_ids: Comma-separated list of Eloqua contact IDs to remove,
                     e.g. "12345,67890".

    Returns:
        JSON confirming removal, or an error message.
    """
    ids = [cid.strip() for cid in contact_ids.split(",") if cid.strip()]
    if not ids:
        return json.dumps({"error": "Provide at least one contact_id."})

    payload = {
        "type": "ContactList",
        "id": list_id,
        "membershipDeletions": [{"type": "Contact", "id": cid} for cid in ids],
    }
    result = _put(f"assets/contact/list/{list_id}", payload)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Engagement / Activity
# ---------------------------------------------------------------------------

@mcp.tool()
def eloqua_get_contact_activity(
    contact_id: str,
    activity_type: str = "",
    start_date: str = "",
    end_date: str = "",
) -> str:
    """
    Get engagement activity data for a contact.

    Returns the contact record at full depth, which includes key engagement
    timestamps: lastModifiedDate, subscriptionDate, bounceback status, and
    recent email interaction metadata stored on the contact record.

    For full granular activity logs (every open/click event), use the Eloqua
    Bulk API export — this tool returns the contact-level summary available
    via the standard REST API.

    Args:
        contact_id: The Eloqua contact ID.
        activity_type: Informational filter label (used in response summary).
        start_date: Informational date range start label (used in response summary).
        end_date: Informational date range end label (used in response summary).

    Returns:
        JSON with the full contact record including engagement-related fields:
        emailAddress, subscriptionDate, bouncedAt, lastModifiedDate,
        and any custom engagement field values.
    """
    result = _get(f"data/contact/{contact_id}", {"depth": "complete"})
    summary = {
        "note": (
            "Eloqua REST API v2 provides contact-level engagement metadata. "
            "For per-event activity logs (EmailOpen, EmailClickthrough, etc.), "
            "use the Eloqua Bulk API export."
        ),
        "filter": {
            "activity_type": activity_type or "all",
            "start_date": start_date,
            "end_date": end_date,
        },
        "contact": result,
    }
    return json.dumps(summary, indent=2)


@mcp.tool()
def eloqua_get_form_submissions(
    form_id: str,
    page: int = 1,
    page_size: int = 50,
) -> str:
    """
    Get form submission records from an Eloqua form — useful for lead scoring and inbound lead capture.

    Args:
        form_id: The Eloqua form asset ID.
        page: Page number (default 1).
        page_size: Results per page (default 50, max 200).

    Returns:
        JSON array of submission records with contact data, submission time,
        and all submitted field values.
    """
    params = {
        "count": min(page_size, 200),
        "page": page,
        "depth": "complete",
    }
    result = _get(f"data/form/{form_id}", params)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Email Performance Reporting
# ---------------------------------------------------------------------------

def _safe_rate(numerator, denominator) -> Optional[str]:
    """Return a percentage string like '23.45%', or None if not computable."""
    try:
        n = float(numerator or 0)
        d = float(denominator or 0)
        if d == 0:
            return None
        return f"{round((n / d) * 100, 2)}%"
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Bulk API helpers for email activity exports
# ---------------------------------------------------------------------------

def _bulk_base_url() -> str:
    """Return the Eloqua Bulk API v2 base URL (e.g. https://secure.p06.eloqua.com/api/bulk/2.0)."""
    rest_base = _resolve_base_url()   # ensures _BASE_URL is set
    global _BASE_URL
    return f"{_BASE_URL}/api/bulk/2.0"


def _bulk_run_sync(activity_type: str, start_dt: str, end_dt: str,
                   campaign_id_filter: str = "") -> tuple:
    """
    Create a Bulk API activity export for one activity type, run a sync,
    poll until done, page through all results.

    start_dt / end_dt must be in 'YYYY-MM-DD HH:MM:SS' format.
    Returns (records: list, error: str) — error is "" on success.

    Field notes:
    - SmtpStatusCode and SmtpMessage are ONLY valid for Bounceback exports.
    - SubjectLine and EmailSendType are only valid for EmailSend exports.
    """
    import time as _time

    try:
        bulk = _bulk_base_url()
    except Exception as exc:
        return [], f"Could not resolve Eloqua Bulk API URL: {exc}"

    hdrs = _headers()
    ts = int(_time.time())

    filter_expr = (
        f"'{{{{Activity.Type}}}}'='{activity_type}' "
        f"AND '{{{{Activity.CreatedAt}}}}'>= '{start_dt}' "
        f"AND '{{{{Activity.CreatedAt}}}}' <= '{end_dt}'"
    )
    if campaign_id_filter:
        filter_expr += f" AND '{{{{Activity.Campaign.Id}}}}'='{campaign_id_filter}'"

    base_fields: dict = {
        "ActivityId":   "{{Activity.Id}}",
        "ActivityType": "{{Activity.Type}}",
        "ActivityDate": "{{Activity.CreatedAt}}",
        "ContactId":    "{{Activity.Contact.Id}}",
        "AssetId":      "{{Activity.Asset.Id}}",
        "AssetName":    "{{Activity.Asset.Name}}",
        "CampaignId":   "{{Activity.Campaign.Id}}",
        "CampaignName": "{{Activity.Campaign.Field(CampaignName)}}",
    }

    extra_fields: dict = {}
    if activity_type == "EmailSend":
        extra_fields["SubjectLine"]   = "{{Activity.Field(SubjectLine)}}"
        extra_fields["EmailSendType"] = "{{Activity.Field(EmailSendType)}}"
    elif activity_type == "Bounceback":
        extra_fields["SmtpStatusCode"] = "{{Activity.Field(SmtpStatusCode)}}"
        extra_fields["SmtpMessage"]    = "{{Activity.Field(SmtpMessage)}}"

    export_def = {
        "name":   f"deepagent_{activity_type}_{ts}",
        "fields": {**base_fields, **extra_fields},
        "filter": filter_expr,
    }

    try:
        with httpx.Client(timeout=90) as client:
            # Step 1: Create export
            r = client.post(f"{bulk}/activities/exports", headers=hdrs, json=export_def)
            if r.status_code >= 400:
                return [], f"Export create failed ({r.status_code}): {r.text[:300]}"
            export_uri = r.json().get("uri", "")
            if not export_uri:
                return [], f"Export create returned no URI. Response: {r.text[:300]}"

            # Step 2: Trigger sync
            r2 = client.post(f"{bulk}/syncs", headers=hdrs, json={"syncedInstanceUri": export_uri})
            if r2.status_code >= 400:
                return [], f"Sync create failed ({r2.status_code}): {r2.text[:300]}"
            sync_uri = r2.json().get("uri", "")
            sync_id = sync_uri.split("/")[-1]
            if not sync_id:
                return [], f"Sync returned no URI. Response: {r2.text[:300]}"

            # Step 3: Poll up to 150 seconds (30 × 5s)
            status = ""
            status_detail = ""
            for _ in range(30):
                _time.sleep(5)
                r3 = client.get(f"{bulk}/syncs/{sync_id}", headers=hdrs)
                body = r3.json()
                status = body.get("status", "")
                status_detail = body.get("statusMessage", "") or body.get("message", "")
                if status in ("success", "warning", "error"):
                    break

            if status == "error":
                return [], f"Sync failed with status=error: {status_detail}"
            if status not in ("success", "warning"):
                return [], f"Sync timed out after 150s — last status='{status}'"

            # Step 4: Page through data
            all_items: list = []
            offset = 0
            page_size = 5000
            while True:
                r4 = client.get(
                    f"{bulk}/syncs/{sync_id}/data",
                    headers=hdrs,
                    params={"limit": page_size, "offset": offset},
                )
                if r4.status_code >= 400:
                    break
                page_data = r4.json()
                items = page_data.get("items", [])
                all_items.extend(items)
                if len(items) < page_size:
                    break
                offset += page_size

            return all_items, ""

    except Exception as exc:
        return [], f"Exception during {activity_type} sync: {exc}"


@mcp.tool()
def eloqua_get_campaign_email_report(
    start_date: str,
    end_date: str,
    campaign_name_filter: str = "",
    max_campaigns: int = 100,
) -> str:
    """
    Get email performance metrics for batch campaigns from Eloqua using the Bulk API.
    Returns rows matching the standard reporting Google Sheet headers:

      Email Send Date | Campaign Name | Email Subject Line | Total Sends |
      Delivered Rate | Unique Open Rate | Unique Clickthrough Rate |
      Bounceback Rate | Hard Bounceback Rate | Soft Bounceback Rate |
      Unsubscribed Rate

    This tool is specifically designed for campaign-based batch sends (e.g. US_ENT_,
    APAC_, EU_, MM_ campaigns). It queries the Eloqua Bulk API for real email
    activity data — NOT the deployments endpoint which only covers quick sends.

    How it works:
      1. Fetches campaigns in the date range matching the optional name filter.
      2. Runs 5 Bulk API activity syncs (EmailSend, EmailOpen, EmailClickthrough,
         EmailBounceback, EmailUnsubscribe) covering the date range.
      3. Aggregates by campaign, computing unique opens/clicks (distinct contacts)
         and splitting bounces into hard (5xx SMTP) vs soft (4xx SMTP).
      4. Returns one row per campaign email send with all rate columns computed.

    Note: The syncs may take 30–120 seconds depending on data volume.

    Args:
        start_date:           Start of date range, YYYY-MM-DD (e.g. '2026-03-01').
        end_date:             End of date range, YYYY-MM-DD (e.g. '2026-03-31').
        campaign_name_filter: Optional substring to filter campaign names
                              (e.g. 'US_ENT' or 'APAC' or 'EU'). Leave empty for all.
        max_campaigns:        Maximum number of campaigns to include (default 100).

    Returns:
        JSON object with 'rows' array — each row is one campaign email send.
    """
    import time as _time
    from collections import defaultdict

    # Convert to 'YYYY-MM-DD HH:MM:SS' for Bulk API filter.
    # Strip any ISO time/timezone suffix first so that inputs like
    # '2026-04-21T00:00:00Z' don't become '2026-04-21T00:00:00Z 00:00:00'.
    start_dt = f"{start_date[:10]} 00:00:00"
    end_dt   = f"{end_date[:10]} 23:59:59"

    # -----------------------------------------------------------------------
    # Step 1: Get campaigns in the date range via REST API
    # -----------------------------------------------------------------------
    all_campaigns = []
    page = 1
    while len(all_campaigns) < max_campaigns:
        params: dict = {"count": 50, "page": page, "depth": "partial", "orderBy": "id DESC"}
        if campaign_name_filter:
            params["search"] = f"name='{campaign_name_filter}*'"
        raw = _get("assets/campaigns", params)
        if isinstance(raw, dict) and "error" in raw:
            return json.dumps(raw, indent=2)
        elements = raw.get("elements", [])
        if not elements:
            break
        all_campaigns.extend(elements)
        if len(elements) < 50:
            break
        page += 1

    if not all_campaigns:
        return json.dumps({"error": f"No campaigns found matching filter '{campaign_name_filter}'."})

    # Build a lookup: campaignId → {name, scheduledFor, startAt}
    campaign_meta: dict = {}
    for c in all_campaigns[:max_campaigns]:
        cid = str(c.get("id", ""))
        campaign_meta[cid] = {
            "name": c.get("name", ""),
            "scheduledFor": c.get("scheduledFor", ""),
            "startAt": c.get("startAt", ""),
        }

    # -----------------------------------------------------------------------
    # Step 2: Run Bulk API syncs for each activity type
    # -----------------------------------------------------------------------
    # NOTE: Eloqua Bulk API uses "Bounceback" / "Unsubscribe" (not the prefixed display names)
    activity_types = ["EmailSend", "EmailOpen", "EmailClickthrough", "Bounceback", "Unsubscribe"]
    raw_activities: dict[str, list] = {}
    for atype in activity_types:
        records, err = _bulk_run_sync(atype, start_dt, end_dt)
        raw_activities[atype] = records
        if err:
            print(f"[Eloqua][_bulk_run_sync] {atype} error: {err}", flush=True)

    # -----------------------------------------------------------------------
    # Step 3: Aggregate by (campaignId, assetId)
    # -----------------------------------------------------------------------
    # key = (campaign_id, asset_id)
    sends:      dict = defaultdict(set)   # contactId set → unique senders (= total sends)
    opens:      dict = defaultdict(set)   # contactId set → unique openers
    clicks:     dict = defaultdict(set)   # contactId set → unique clickers
    hard_bb:    dict = defaultdict(int)   # count of hard bounces
    soft_bb:    dict = defaultdict(int)   # count of soft bounces
    unsubs:     dict = defaultdict(int)   # count of unsubscribes
    meta:       dict = {}                 # key → {subjectLine, campaignName, assetName, activityDate}

    for rec in raw_activities.get("EmailSend", []):
        cid   = str(rec.get("CampaignId", "") or "")
        aid   = str(rec.get("AssetId", "") or "")
        ctct  = str(rec.get("ContactId", "") or "")
        if not cid or not aid:
            continue
        k = (cid, aid)
        sends[k].add(ctct)
        if k not in meta:
            meta[k] = {
                "subjectLine":  rec.get("SubjectLine", ""),
                "campaignName": rec.get("CampaignName", "") or campaign_meta.get(cid, {}).get("name", ""),
                "assetName":    rec.get("AssetName", ""),
                "activityDate": rec.get("ActivityDate", ""),
            }

    for rec in raw_activities.get("EmailOpen", []):
        cid  = str(rec.get("CampaignId", "") or "")
        aid  = str(rec.get("AssetId", "") or "")
        ctct = str(rec.get("ContactId", "") or "")
        if cid and aid:
            opens[(cid, aid)].add(ctct)

    for rec in raw_activities.get("EmailClickthrough", []):
        cid  = str(rec.get("CampaignId", "") or "")
        aid  = str(rec.get("AssetId", "") or "")
        ctct = str(rec.get("ContactId", "") or "")
        if cid and aid:
            clicks[(cid, aid)].add(ctct)

    for rec in raw_activities.get("Bounceback", []):
        cid    = str(rec.get("CampaignId", "") or "")
        aid    = str(rec.get("AssetId", "") or "")
        smtp   = str(rec.get("SmtpStatusCode", "") or "")
        if not cid or not aid:
            continue
        k = (cid, aid)
        # 5xx = hard bounce, 4xx = soft bounce
        if smtp.startswith("5"):
            hard_bb[k] += 1
        else:
            soft_bb[k] += 1

    for rec in raw_activities.get("Unsubscribe", []):
        cid = str(rec.get("CampaignId", "") or "")
        aid = str(rec.get("AssetId", "") or "")
        if cid and aid:
            unsubs[(cid, aid)] += 1

    # -----------------------------------------------------------------------
    # Step 4: Build output rows
    # -----------------------------------------------------------------------
    rows = []
    for k, senders in sends.items():
        cid, aid = k
        total_sends  = len(senders)
        n_hard       = hard_bb.get(k, 0)
        n_soft       = soft_bb.get(k, 0)
        n_bounce     = n_hard + n_soft
        n_opens      = len(opens.get(k, set()))
        n_clicks     = len(clicks.get(k, set()))
        n_unsub      = unsubs.get(k, 0)
        delivered    = total_sends - n_bounce

        m            = meta.get(k, {})
        c_meta       = campaign_meta.get(cid, {})
        send_date    = m.get("activityDate", "") or c_meta.get("scheduledFor", "") or c_meta.get("startAt", "")

        rows.append({
            "Email Send Date":           send_date,
            "Campaign Name":             m.get("campaignName") or c_meta.get("name", ""),
            "Email Subject Line":        m.get("subjectLine", ""),
            "Total Sends":               total_sends,
            "Delivered Rate":            _safe_rate(delivered, total_sends),
            "Unique Open Rate":          _safe_rate(n_opens, total_sends),
            "Unique Clickthrough Rate":  _safe_rate(n_clicks, total_sends),
            "Bounceback Rate":           _safe_rate(n_bounce, total_sends),
            "Hard Bounceback Rate":      _safe_rate(n_hard, total_sends),
            "Soft Bounceback Rate":      _safe_rate(n_soft, total_sends),
            "Unsubscribed Rate":         _safe_rate(n_unsub, total_sends),
            "_ids": {"campaign_id": cid, "email_id": aid},
        })

    # Sort by send date descending
    rows.sort(key=lambda r: r.get("Email Send Date", "") or "", reverse=True)

    activity_counts = {atype: len(recs) for atype, recs in raw_activities.items()}
    return json.dumps({
        "date_range": {"start": start_date, "end": end_date},
        "campaigns_found": len(campaign_meta),
        "rows": len(rows),
        "activity_records_pulled": activity_counts,
        "data": rows,
    }, indent=2)


@mcp.tool()
def eloqua_get_email_performance(
    campaign_name: str = "",
    start_date: str = "",
    end_date: str = "",
    page: int = 1,
    page_size: int = 50,
) -> str:
    """
    Get email deployment performance metrics from Eloqua, formatted as rows
    matching the standard reporting sheet headers:

      Email Send Date | Campaign Name | Email Subject Line | Total Sends |
      Delivered Rate | Unique Open Rate | Unique Clickthrough Rate |
      Bounceback Rate | Hard Bounceback Rate | Soft Bounceback Rate |
      Unsubscribed Rate

    All rates are expressed as percentages (e.g. '23.45%') calculated against
    Total Sends. Returns one row per email deployment (i.e. one campaign send).

    Args:
        campaign_name: Optional keyword to filter by campaign name.
        start_date:    Optional ISO date string (YYYY-MM-DD) — only deployments
                       sent on or after this date are returned.
        end_date:      Optional ISO date string (YYYY-MM-DD) — only deployments
                       sent on or before this date are returned.
        page:          Page number (default 1).
        page_size:     Results per page (default 50, max 200).

    Returns:
        JSON array of objects, each with keys matching the sheet column headers,
        plus a raw_stats block for reference.
    """
    params: dict = {
        "count": min(page_size, 200),
        "page": page,
        "depth": "complete",
    }
    search_parts = []
    if campaign_name:
        search_parts.append(f"name='{campaign_name}'")
    if start_date:
        search_parts.append(f"sentAt>'{start_date}'")
    if end_date:
        search_parts.append(f"sentAt<'{end_date}'")
    if search_parts:
        params["search"] = " AND ".join(search_parts)

    raw = _get("assets/email/deployments", params)

    if isinstance(raw, dict) and "error" in raw:
        return json.dumps(raw, indent=2)

    elements = raw if isinstance(raw, list) else raw.get("elements", [])

    rows = []
    for dep in elements:
        total_sends = int(dep.get("totalSendCount") or dep.get("sentCount") or 0)
        delivered   = int(dep.get("deliveredCount") or 0)
        bouncebacks = int(dep.get("bouncebackCount") or dep.get("totalBouncebackCount") or 0)
        hard_bb     = int(dep.get("hardBouncebackCount") or 0)
        soft_bb     = int(dep.get("softBouncebackCount") or 0)
        unique_opens= int(dep.get("uniqueOpens") or dep.get("openCount") or 0)
        unique_clicks= int(dep.get("uniqueClickthroughs") or dep.get("clickthroughCount") or 0)
        unsubs      = int(dep.get("unsubscribes") or dep.get("unsubscribeCount") or 0)

        # Delivered = total sends minus bouncebacks (if API doesn't provide it directly)
        if delivered == 0 and total_sends > 0:
            delivered = total_sends - bouncebacks

        rows.append({
            "Email Send Date":           dep.get("sentAt") or dep.get("startAt") or dep.get("scheduledFor") or "",
            "Campaign Name":             dep.get("campaignName") or dep.get("name") or "",
            "Email Subject Line":        dep.get("subject") or "",
            "Total Sends":               total_sends,
            "Delivered Rate":            _safe_rate(delivered, total_sends),
            "Unique Open Rate":          _safe_rate(unique_opens, total_sends),
            "Unique Clickthrough Rate":  _safe_rate(unique_clicks, total_sends),
            "Bounceback Rate":           _safe_rate(bouncebacks, total_sends),
            "Hard Bounceback Rate":      _safe_rate(hard_bb, total_sends),
            "Soft Bounceback Rate":      _safe_rate(soft_bb, total_sends),
            "Unsubscribed Rate":         _safe_rate(unsubs, total_sends),
            "_raw": {
                "deployment_id":  dep.get("id"),
                "email_id":       dep.get("emailId"),
                "delivered":      delivered,
                "unique_opens":   unique_opens,
                "unique_clicks":  unique_clicks,
                "bouncebacks":    bouncebacks,
                "hard_bb":        hard_bb,
                "soft_bb":        soft_bb,
                "unsubscribes":   unsubs,
            },
        })

    return json.dumps({"total": len(rows), "rows": rows}, indent=2)


@mcp.tool()
def eloqua_get_email_deployment(deployment_id: str) -> str:
    """
    Get full details and send statistics for a single Eloqua email deployment.

    Useful for drilling into one specific send to see all raw metric fields
    returned by the Eloqua API (helpful for debugging or mapping new fields).

    Args:
        deployment_id: The Eloqua email deployment ID.

    Returns:
        JSON with the full deployment record including all available stats.
    """
    result = _get(f"assets/email/deployment/{deployment_id}", {"depth": "complete"})
    return json.dumps(result, indent=2)


@mcp.tool()
def eloqua_campaign_performance_board(
    start_date: str,
    end_date: str,
    campaign_name_filter: str = "",
    max_campaigns: int = 200,
) -> str:
    """
    Pull a complete campaign performance board for a MULTI-DAY date range in ONE call.

    Runs all 5 Bulk API activity syncs (EmailSend, EmailOpen, EmailClickthrough,
    Bounceback, Unsubscribe) CONCURRENTLY instead of sequentially, reducing
    wall-clock time from ~10 minutes to ~2 minutes for a typical 10-day window.

    Returns a COMPACT CSV string (~5% of the context tokens vs the JSON version).
    Use this tool for ANY multi-day Eloqua email performance query. Do NOT call
    eloqua_get_campaign_email_report for date ranges — it is sequential and slow.

    Args:
        start_date:           Start of date range, YYYY-MM-DD (e.g. '2026-04-21').
        end_date:             End of date range, YYYY-MM-DD (e.g. '2026-04-30').
        campaign_name_filter: Optional substring to filter campaign names
                              (e.g. 'US_ENT' or 'APAC' or 'EU'). Leave empty for all.
        max_campaigns:        Maximum campaigns to include (default 200).

    Returns:
        A CSV string. Columns:
          send_date, campaign_name, subject_line,
          total_sends, delivered, delivered_rate_pct,
          unique_opens, open_rate_pct,
          unique_clicks, click_rate_pct,
          hard_bounces, soft_bounces, bounce_rate_pct,
          unsubscribes, unsub_rate_pct
        First row = header. Last row = TOTAL across all campaigns.
    """
    import io, csv as _csv, time as _time
    from concurrent.futures import ThreadPoolExecutor
    from collections import defaultdict

    # Strip ISO time/timezone suffix before appending time component so that
    # inputs like '2026-04-21T00:00:00Z' don't become '2026-04-21T00:00:00Z 00:00:00'.
    start_dt = f"{start_date[:10]} 00:00:00"
    end_dt   = f"{end_date[:10]} 23:59:59"

    # ── 1. Run all 5 Bulk API syncs concurrently ───────────────────────────
    activity_types = ["EmailSend", "EmailOpen", "EmailClickthrough", "Bounceback", "Unsubscribe"]

    def _fetch_activity(atype):
        records, err = _bulk_run_sync(atype, start_dt, end_dt)
        return atype, records, err

    raw_activities: dict = {}
    sync_errors: dict = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        for atype, records, err in pool.map(_fetch_activity, activity_types):
            raw_activities[atype] = records
            if err:
                sync_errors[atype] = err

    # Surface sync errors as a diagnostic block so Claude can report them
    if sync_errors:
        error_lines = "\n".join(f"  {k}: {v}" for k, v in sync_errors.items())
        if not raw_activities.get("EmailSend"):
            return (
                f"ERROR: All critical Bulk API syncs failed for {start_date} to {end_date}.\n\n"
                f"Sync errors:\n{error_lines}\n\n"
                f"Possible causes:\n"
                f"  - Eloqua credentials (ELOQUA_SITE_NAME / ELOQUA_USERNAME / ELOQUA_PASSWORD) are wrong or expired\n"
                f"  - Eloqua Bulk API is not enabled for this account\n"
                f"  - No email send activity exists in this date range\n"
                f"  - Network connectivity issue to Eloqua servers"
            )

    if not raw_activities.get("EmailSend"):
        diag = (
            f"\nSync diagnostics: "
            + ", ".join(f"{k}={len(v)} records" for k, v in raw_activities.items())
        )
        return f"No EmailSend activity found between {start_date} and {end_date}.{diag}"

    # ── 2. Aggregate by (campaign_id, asset_id) ───────────────────────────
    sends   = defaultdict(set)
    opens   = defaultdict(set)
    clicks  = defaultdict(set)
    hard_bb = defaultdict(int)
    soft_bb = defaultdict(int)
    unsubs  = defaultdict(int)
    meta    = {}

    for rec in raw_activities.get("EmailSend", []):
        cid  = str(rec.get("CampaignId", "") or "")
        aid  = str(rec.get("AssetId", "") or "")
        ctct = str(rec.get("ContactId", "") or "")
        if not cid or not aid:
            continue
        k = (cid, aid)
        if campaign_name_filter:
            cname = rec.get("CampaignName", "") or ""
            if campaign_name_filter.lower() not in cname.lower():
                continue
        sends[k].add(ctct)
        if k not in meta:
            meta[k] = {
                "subjectLine":  rec.get("SubjectLine", ""),
                "campaignName": rec.get("CampaignName", ""),
                "activityDate": (rec.get("ActivityDate", "") or "")[:10],
            }

    for rec in raw_activities.get("EmailOpen", []):
        cid  = str(rec.get("CampaignId", "") or "")
        aid  = str(rec.get("AssetId", "") or "")
        ctct = str(rec.get("ContactId", "") or "")
        if cid and aid and (cid, aid) in sends:
            opens[(cid, aid)].add(ctct)

    for rec in raw_activities.get("EmailClickthrough", []):
        cid  = str(rec.get("CampaignId", "") or "")
        aid  = str(rec.get("AssetId", "") or "")
        ctct = str(rec.get("ContactId", "") or "")
        if cid and aid and (cid, aid) in sends:
            clicks[(cid, aid)].add(ctct)

    for rec in raw_activities.get("Bounceback", []):
        cid  = str(rec.get("CampaignId", "") or "")
        aid  = str(rec.get("AssetId", "") or "")
        smtp = str(rec.get("SmtpStatusCode", "") or "")
        if not cid or not aid or (cid, aid) not in sends:
            continue
        k = (cid, aid)
        if smtp.startswith("5"):
            hard_bb[k] += 1
        else:
            soft_bb[k] += 1

    for rec in raw_activities.get("Unsubscribe", []):
        cid = str(rec.get("CampaignId", "") or "")
        aid = str(rec.get("AssetId", "") or "")
        if cid and aid and (cid, aid) in sends:
            unsubs[(cid, aid)] += 1

    # ── 3. Build rows ──────────────────────────────────────────────────────
    def _pct(n, d):
        return round(n / d * 100, 2) if d else 0.0

    rows = []
    for k, senders in sends.items():
        total  = len(senders)
        hb     = hard_bb.get(k, 0)
        sb     = soft_bb.get(k, 0)
        nb     = hb + sb
        deliv  = max(total - nb, 0)
        no     = len(opens.get(k, set()))
        nc     = len(clicks.get(k, set()))
        nu     = unsubs.get(k, 0)
        m      = meta.get(k, {})
        rows.append({
            "send_date":         m.get("activityDate", ""),
            "campaign_name":     m.get("campaignName", ""),
            "subject_line":      m.get("subjectLine", ""),
            "total_sends":       total,
            "delivered":         deliv,
            "delivered_rate_pct": _pct(deliv, total),
            "unique_opens":      no,
            "open_rate_pct":     _pct(no, total),
            "unique_clicks":     nc,
            "click_rate_pct":    _pct(nc, total),
            "hard_bounces":      hb,
            "soft_bounces":      sb,
            "bounce_rate_pct":   _pct(nb, total),
            "unsubscribes":      nu,
            "unsub_rate_pct":    _pct(nu, total),
        })

    rows.sort(key=lambda r: (r["send_date"], r["campaign_name"]))
    rows = rows[:max_campaigns]

    # ── 4. Totals row ─────────────────────────────────────────────────────
    t_sends  = sum(r["total_sends"]   for r in rows)
    t_deliv  = sum(r["delivered"]     for r in rows)
    t_opens  = sum(r["unique_opens"]  for r in rows)
    t_clicks = sum(r["unique_clicks"] for r in rows)
    t_hb     = sum(r["hard_bounces"]  for r in rows)
    t_sb     = sum(r["soft_bounces"]  for r in rows)
    t_nb     = t_hb + t_sb
    t_unsub  = sum(r["unsubscribes"]  for r in rows)

    # ── 5. Build CSV ───────────────────────────────────────────────────────
    buf = io.StringIO()
    writer = _csv.writer(buf)
    cols = [
        "send_date", "campaign_name", "subject_line",
        "total_sends", "delivered", "delivered_rate_pct",
        "unique_opens", "open_rate_pct",
        "unique_clicks", "click_rate_pct",
        "hard_bounces", "soft_bounces", "bounce_rate_pct",
        "unsubscribes", "unsub_rate_pct",
    ]
    writer.writerow(cols)
    for r in rows:
        writer.writerow([r[c] for c in cols])

    writer.writerow([
        f"{start_date} to {end_date}", "TOTAL", "",
        t_sends, t_deliv, _pct(t_deliv, t_sends),
        t_opens, _pct(t_opens, t_sends),
        t_clicks, _pct(t_clicks, t_sends),
        t_hb, t_sb, _pct(t_nb, t_sends),
        t_unsub, _pct(t_unsub, t_sends),
    ])

    result = buf.getvalue()
    return (
        f"# Eloqua Campaign Performance Board\n"
        f"# Range: {start_date} to {end_date}"
        + (f" | Filter: {campaign_name_filter}" if campaign_name_filter else "")
        + f"\n# Campaigns: {len(rows)} | Total sends: {t_sends:,}\n\n"
        + result
    )


if __name__ == "__main__":
    try:
        _resolve_base_url()
        print(f"[Eloqua] Authenticated. REST base: {_REST_BASE}", flush=True)
    except Exception as exc:
        print(f"[Eloqua] Warning: could not resolve base URL at startup: {exc}", flush=True)

    mcp.run(transport="stdio")
