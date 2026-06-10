import os
import json
import time
import threading
from typing import Optional, List, Any

import httpx
from fastmcp import FastMCP

API_KEY = os.environ.get("MAILCHIMP_API_KEY", "")

def _get_base_url() -> str:
    if not API_KEY:
        raise RuntimeError(
            "MAILCHIMP_API_KEY environment variable is not set. "
            "Please set it to your Mailchimp API key (e.g. 'abc123-us6')."
        )
    parts = API_KEY.split("-")
    if len(parts) < 2:
        raise RuntimeError(
            "Invalid MAILCHIMP_API_KEY format. The key must end with a datacenter suffix "
            "like '-us1' or '-us6' (e.g. 'abc123def456-us6')."
        )
    dc = parts[-1]
    return f"https://{dc}.api.mailchimp.com/3.0"


def _headers() -> dict:
    if not API_KEY:
        raise RuntimeError("MAILCHIMP_API_KEY environment variable is not set.")
    return {
        "Authorization": f"Basic {_basic_auth()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _basic_auth() -> str:
    import base64
    return base64.b64encode(f"anystring:{API_KEY}".encode()).decode()


# Mailchimp allows 10 simultaneous connections per API key
_api_semaphore = threading.Semaphore(10)
# Separate lock guards _last_request_time only — prevents races on the throttle counter
_throttle_lock = threading.Lock()
_last_request_time = 0.0
_MIN_REQUEST_INTERVAL = 0.05   # 50 ms between requests = 20 req/s upper bound


def _throttle():
    """Rate-limit without holding _api_semaphore — safe to call from any thread."""
    global _last_request_time
    with _throttle_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < _MIN_REQUEST_INTERVAL:
            time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
        _last_request_time = time.monotonic()


def _request_with_retry(method: str, path: str, json_payload=None,
                        params=None, max_retries: int = 3) -> httpx.Response:
    url = f"{_get_base_url()}{path}"
    headers = _headers()
    resp = None
    for attempt in range(max_retries):
        try:
            with _api_semaphore:
                _throttle()
                with httpx.Client(timeout=30) as client:
                    if method == "GET":
                        resp = client.get(url, headers=headers, params=params)
                    elif method == "POST":
                        resp = client.post(url, headers=headers, json=json_payload or {}, params=params)
                    elif method == "PATCH":
                        resp = client.patch(url, headers=headers, json=json_payload or {})
                    elif method == "PUT":
                        resp = client.put(url, headers=headers, json=json_payload or {})
                    elif method == "DELETE":
                        resp = client.delete(url, headers=headers)
                    else:
                        raise ValueError(f"Unsupported HTTP method: {method}")
        except httpx.TimeoutException:
            if attempt < max_retries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise

        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", 2.0 * (attempt + 1)))
            if attempt < max_retries - 1:
                time.sleep(wait)
                continue
        return resp
    return resp


def _raise_with_detail(resp: httpx.Response):
    if resp.status_code >= 400:
        try:
            body = resp.text
        except Exception:
            body = ""
        raise RuntimeError(
            f"Mailchimp API error {resp.status_code} "
            f"for {resp.request.method} {resp.url}: {body}"
        )


def _get(path: str, params: Optional[dict] = None) -> Any:
    resp = _request_with_retry("GET", path, params=params)
    _raise_with_detail(resp)
    if not resp.text or not resp.text.strip():
        return None
    return resp.json()


def _fetch_report(cid: str) -> tuple:
    """Fetch a single campaign report with a short timeout and no retry.

    Returns (cid, report_dict). On any error returns (cid, {}) so the caller
    can still build a row with zeroed-out metrics rather than blowing up.
    """
    url = f"{_get_base_url()}/reports/{cid}"
    headers = _headers()
    with _api_semaphore:
        _throttle()
        try:
            with httpx.Client(timeout=12) as client:
                resp = client.get(url, headers=headers)
            if resp.status_code >= 400:
                return cid, {}
            if not resp.text or not resp.text.strip():
                return cid, {}
            return cid, resp.json()
        except Exception:
            return cid, {}


def _fetch_campaign_page(params: dict) -> Optional[dict]:
    """Fetch one page of the /campaigns list.

    Uses a tight 20 s timeout with no retry — designed to fail fast so the
    caller can break out of paging and work with whatever it has collected so
    far rather than hanging for 90+ seconds on a slow Mailchimp response.

    Returns the parsed JSON dict, or None on any error / timeout.
    """
    url = f"{_get_base_url()}/campaigns"
    headers = _headers()
    try:
        _throttle()
        with httpx.Client(timeout=20) as client:
            resp = client.get(url, headers=headers, params=params)
        if resp.status_code >= 400 or not resp.text or not resp.text.strip():
            return None
        return resp.json()
    except Exception:
        return None


def _fetch_reports_page(params: dict) -> Optional[dict]:
    """Fetch one page of the /reports endpoint (campaign-level performance data).

    The /reports endpoint returns opens, clicks, bounces, and unsubscribes
    directly — no per-campaign follow-up call needed.  Uses a 20 s timeout
    with no retry so a slow Mailchimp response fails fast and the caller can
    return partial results rather than hanging.

    Returns the parsed JSON dict, or None on any error / timeout.
    """
    url = f"{_get_base_url()}/reports"
    headers = _headers()
    try:
        _throttle()
        with httpx.Client(timeout=20) as client:
            resp = client.get(url, headers=headers, params=params)
        if resp.status_code >= 400 or not resp.text or not resp.text.strip():
            return None
        return resp.json()
    except Exception:
        return None


BOT_CLICK_GAP_SECONDS = 2


def _classify_clickers(campaign_id: str) -> dict:
    """
    Paginate /reports/{cid}/email-activity and classify every subscriber who clicked
    as either 'human' or 'bot' using timing analysis.

    Algorithm:
      - A subscriber is a BOT if they clicked MORE THAN ONE link AND every click
        happened within BOT_CLICK_GAP_SECONDS of each other (security-scanner pattern).
      - A subscriber is HUMAN if they clicked only one link, or if at least one gap
        between consecutive clicks is >= BOT_CLICK_GAP_SECONDS.

    Returns:
        dict keyed by email_address with value dict:
          {
            "is_human": bool,
            "click_count": int,
            "timestamps": [str, ...],   # ISO8601
          }
        Returns empty dict on API error.
    """
    from datetime import datetime as _dt

    clicker_clicks: dict = {}
    offset = 0
    while True:
        try:
            page = _get(f"/reports/{campaign_id}/email-activity", params={
                "count": "1000",
                "offset": str(offset),
                "fields": "emails.email_address,emails.activity,total_items",
            })
        except Exception:
            return {}
        if not page:
            break
        emails = page.get("emails", [])
        if not emails:
            break
        for entry in emails:
            email = entry.get("email_address", "")
            for act in entry.get("activity", []):
                if act.get("action") == "click" and act.get("timestamp"):
                    clicker_clicks.setdefault(email, []).append(act["timestamp"])
        if len(clicker_clicks) >= page.get("total_items", 0) or not emails:
            break
        offset += 1000
        if offset >= page.get("total_items", 10000):
            break

    result = {}
    for email, timestamps in clicker_clicks.items():
        if len(timestamps) == 1:
            is_human = True
        else:
            try:
                times = sorted(_dt.fromisoformat(ts.replace("Z", "+00:00")) for ts in timestamps)
                max_gap = max((times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1))
                is_human = max_gap >= BOT_CLICK_GAP_SECONDS
            except Exception:
                is_human = True
        result[email] = {
            "is_human": is_human,
            "click_count": len(timestamps),
            "timestamps": sorted(timestamps),
        }
    return result


def _bot_filtered_clickers(campaign_id: str, raw_unique_subscriber_clicks: int) -> int:
    """
    Return the estimated human (bot-filtered) unique clicker count for a campaign.
    Falls back to raw_unique_subscriber_clicks on API error.
    """
    if raw_unique_subscriber_clicks == 0:
        return 0
    classified = _classify_clickers(campaign_id)
    if not classified:
        return raw_unique_subscriber_clicks
    return sum(1 for v in classified.values() if v["is_human"])


def _post(path: str, payload=None) -> Any:
    resp = _request_with_retry("POST", path, json_payload=payload)
    _raise_with_detail(resp)
    try:
        return resp.json()
    except Exception:
        return {"status": "ok", "status_code": resp.status_code}


def _patch(path: str, payload: dict) -> Any:
    resp = _request_with_retry("PATCH", path, json_payload=payload)
    _raise_with_detail(resp)
    try:
        return resp.json()
    except Exception:
        return {"status": "ok", "status_code": resp.status_code}


def _put(path: str, payload: dict) -> Any:
    resp = _request_with_retry("PUT", path, json_payload=payload)
    _raise_with_detail(resp)
    try:
        return resp.json()
    except Exception:
        return {"status": "ok", "status_code": resp.status_code}


def _delete(path: str) -> Any:
    resp = _request_with_retry("DELETE", path)
    _raise_with_detail(resp)
    if resp.status_code == 204:
        return {"status": "deleted"}
    try:
        return resp.json()
    except Exception:
        return {"status": "ok", "status_code": resp.status_code}


mcp = FastMCP(
    name="Mailchimp",
    instructions=(
        "Use this server to interact with the Mailchimp Marketing API (v3.0). "
        "You can manage audiences/lists and their members, campaigns, templates, "
        "automations, and reports. All operations require a valid Mailchimp API key "
        "set via the MAILCHIMP_API_KEY environment variable. "
        "The API key format is '<key>-<datacenter>' (e.g. 'abc123-us6'). "
        "Use mailchimp_ping to verify connectivity before other operations."
    ),
)


# ---------------------------------------------------------------------------
# Ping / Account
# ---------------------------------------------------------------------------

@mcp.tool()
def mailchimp_ping() -> str:
    """
    Ping the Mailchimp API to verify connectivity and credentials.

    Returns:
        JSON string confirming the connection is healthy.
    """
    result = _get("/ping")
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_get_account_info() -> str:
    """
    Get information about the connected Mailchimp account including account name,
    email address, total subscribers, industry, and plan details.

    Returns:
        JSON string with account details.
    """
    result = _get("/")
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Lists / Audiences
# ---------------------------------------------------------------------------

@mcp.tool()
def mailchimp_list_audiences(
    count: Optional[int] = None,
    offset: Optional[int] = None,
    fields: Optional[str] = None,
    exclude_fields: Optional[str] = None,
) -> str:
    """
    List all audiences (mailing lists) in the Mailchimp account.

    Args:
        count: Number of records to return (max 1000). Default 10.
        offset: Number of records to skip for pagination. Default 0.
        fields: Comma-separated list of fields to include in the response.
        exclude_fields: Comma-separated list of fields to exclude from the response.

    Returns:
        JSON string with array of audience objects including id, name, stats.
    """
    params = {}
    if count is not None:
        params["count"] = count
    if offset is not None:
        params["offset"] = offset
    if fields:
        params["fields"] = fields
    if exclude_fields:
        params["exclude_fields"] = exclude_fields
    result = _get("/lists", params=params or None)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_get_audience(list_id: str) -> str:
    """
    Get details of a specific audience (mailing list) by its ID.

    Args:
        list_id: The unique ID of the Mailchimp audience/list.

    Returns:
        JSON string with audience details including name, stats, settings.
    """
    result = _get(f"/lists/{list_id}")
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_create_audience(
    name: str,
    permission_reminder: str,
    from_name: str,
    from_email: str,
    subject: str,
    language: str = "en",
    email_type_option: bool = False,
) -> str:
    """
    Create a new audience (mailing list).

    Args:
        name: The name of the audience.
        permission_reminder: A description of why contacts are subscribed
            (e.g. "You signed up on our website").
        from_name: The default sender name shown on campaigns.
        from_email: The default reply-to email address for campaigns.
        subject: The default subject line for campaigns.
        language: The default language for the list (default: "en").
        email_type_option: Whether to allow subscribers to choose email format
            (HTML vs plain text). Default False.

    Returns:
        JSON string with the newly created audience object including its id.
    """
    payload = {
        "name": name,
        "permission_reminder": permission_reminder,
        "email_type_option": email_type_option,
        "contact": {
            "company": from_name,
            "address1": "",
            "city": "",
            "state": "",
            "zip": "",
            "country": "US",
        },
        "campaign_defaults": {
            "from_name": from_name,
            "from_email": from_email,
            "subject": subject,
            "language": language,
        },
    }
    result = _post("/lists", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_update_audience(
    list_id: str,
    name: Optional[str] = None,
    permission_reminder: Optional[str] = None,
    from_name: Optional[str] = None,
    from_email: Optional[str] = None,
    subject: Optional[str] = None,
) -> str:
    """
    Update an existing audience (mailing list).

    Args:
        list_id: The unique ID of the audience to update.
        name: New name for the audience (optional).
        permission_reminder: Updated permission reminder text (optional).
        from_name: Updated default sender name (optional).
        from_email: Updated default reply-to email (optional).
        subject: Updated default subject line (optional).

    Returns:
        JSON string with the updated audience object.
    """
    payload: dict = {}
    if name is not None:
        payload["name"] = name
    if permission_reminder is not None:
        payload["permission_reminder"] = permission_reminder
    campaign_defaults: dict = {}
    if from_name is not None:
        campaign_defaults["from_name"] = from_name
    if from_email is not None:
        campaign_defaults["from_email"] = from_email
    if subject is not None:
        campaign_defaults["subject"] = subject
    if campaign_defaults:
        payload["campaign_defaults"] = campaign_defaults
    if not payload:
        raise RuntimeError("No fields provided to update.")
    result = _patch(f"/lists/{list_id}", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_delete_audience(list_id: str) -> str:
    """
    Permanently delete an audience (mailing list) and all its data.
    This action cannot be undone.

    Args:
        list_id: The unique ID of the audience to delete.

    Returns:
        JSON string confirming deletion.
    """
    result = _delete(f"/lists/{list_id}")
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Audience Members
# ---------------------------------------------------------------------------

@mcp.tool()
def mailchimp_list_members(
    list_id: str,
    status: Optional[str] = None,
    count: Optional[int] = None,
    offset: Optional[int] = None,
    email_address: Optional[str] = None,
    since_last_changed: Optional[str] = None,
) -> str:
    """
    List members in an audience.

    Args:
        list_id: The unique ID of the audience.
        status: Filter by subscription status: "subscribed", "unsubscribed",
            "cleaned", "pending", "transactional", or "archived". Optional.
        count: Number of records to return (max 1000). Default 10.
        offset: Number of records to skip for pagination. Default 0.
        email_address: Filter by email address (partial match supported).
        since_last_changed: Filter members whose last change is after this date
            (ISO 8601 format, e.g. "2024-01-01T00:00:00+00:00").

    Returns:
        JSON string with array of member objects.
    """
    params: dict = {}
    if status:
        params["status"] = status
    if count is not None:
        params["count"] = count
    if offset is not None:
        params["offset"] = offset
    if email_address:
        params["email_address"] = email_address
    if since_last_changed:
        params["since_last_changed"] = since_last_changed
    result = _get(f"/lists/{list_id}/members", params=params or None)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_get_member(list_id: str, subscriber_hash: str) -> str:
    """
    Get details for a specific member in an audience.

    The subscriber_hash is the MD5 hash of the lowercase version of the
    member's email address. You can also pass the plain email address and
    this tool will compute the hash automatically.

    Args:
        list_id: The unique ID of the audience.
        subscriber_hash: The MD5 hash of the member's lowercase email address,
            or the plain email address (the tool will hash it for you).

    Returns:
        JSON string with member details including status, merge fields, tags.
    """
    import hashlib
    if "@" in subscriber_hash:
        subscriber_hash = hashlib.md5(subscriber_hash.lower().encode()).hexdigest()
    result = _get(f"/lists/{list_id}/members/{subscriber_hash}")
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_add_or_update_member(
    list_id: str,
    email_address: str,
    status: str = "subscribed",
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    phone: Optional[str] = None,
    address: Optional[dict] = None,
    tags: Optional[List[str]] = None,
    merge_fields: Optional[dict] = None,
    vip: bool = False,
    language: Optional[str] = None,
) -> str:
    """
    Add a new member to an audience or update an existing one (upsert).

    If the email already exists, it will be updated. If not, it will be added.

    Args:
        list_id: The unique ID of the audience.
        email_address: The email address of the member.
        status: Subscription status: "subscribed", "unsubscribed", "cleaned",
            "pending", or "transactional". Default: "subscribed".
        first_name: Member's first name (maps to FNAME merge field).
        last_name: Member's last name (maps to LNAME merge field).
        phone: Member's phone number (maps to PHONE merge field).
        address: Member's address as a dict with optional keys:
            addr1, addr2, city, state, zip, country. Optional.
        tags: List of tag names to apply to the member. Optional.
        merge_fields: Additional merge field key-value pairs (e.g.
            {"COMPANY": "Acme Inc", "BIRTHDAY": "01/15"}). Optional.
        vip: Mark the member as a VIP. Default False.
        language: Member's language code (e.g. "en", "fr", "de"). Optional.

    Returns:
        JSON string with the member object.
    """
    import hashlib
    subscriber_hash = hashlib.md5(email_address.lower().encode()).hexdigest()

    merge = {}
    if first_name:
        merge["FNAME"] = first_name
    if last_name:
        merge["LNAME"] = last_name
    if phone:
        merge["PHONE"] = phone
    if address:
        merge["ADDRESS"] = address
    if merge_fields:
        merge.update(merge_fields)

    payload: dict = {
        "email_address": email_address,
        "status_if_new": status,
        "status": status,
        "vip": vip,
    }
    if merge:
        payload["merge_fields"] = merge
    if tags:
        payload["tags"] = tags
    if language:
        payload["language"] = language

    result = _put(f"/lists/{list_id}/members/{subscriber_hash}", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_update_member(
    list_id: str,
    email_address: str,
    status: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    phone: Optional[str] = None,
    merge_fields: Optional[dict] = None,
    vip: Optional[bool] = None,
    language: Optional[str] = None,
) -> str:
    """
    Update specific fields of an existing member without replacing all data.

    Args:
        list_id: The unique ID of the audience.
        email_address: The email address of the member to update.
        status: New subscription status: "subscribed", "unsubscribed",
            "cleaned", "pending", or "transactional". Optional.
        first_name: Updated first name. Optional.
        last_name: Updated last name. Optional.
        phone: Updated phone number. Optional.
        merge_fields: Additional merge field key-value pairs to update. Optional.
        vip: Updated VIP status. Optional.
        language: Updated language code. Optional.

    Returns:
        JSON string with the updated member object.
    """
    import hashlib
    subscriber_hash = hashlib.md5(email_address.lower().encode()).hexdigest()

    payload: dict = {}
    if status is not None:
        payload["status"] = status
    if vip is not None:
        payload["vip"] = vip
    if language is not None:
        payload["language"] = language

    merge: dict = {}
    if first_name is not None:
        merge["FNAME"] = first_name
    if last_name is not None:
        merge["LNAME"] = last_name
    if phone is not None:
        merge["PHONE"] = phone
    if merge_fields:
        merge.update(merge_fields)
    if merge:
        payload["merge_fields"] = merge

    if not payload:
        raise RuntimeError("No fields provided to update.")

    result = _patch(f"/lists/{list_id}/members/{subscriber_hash}", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_archive_member(list_id: str, email_address: str) -> str:
    """
    Archive a member from an audience (soft delete). Archived members can
    be re-added later. To permanently delete, use mailchimp_delete_member.

    Args:
        list_id: The unique ID of the audience.
        email_address: The email address of the member to archive.

    Returns:
        JSON string confirming the member was archived.
    """
    import hashlib
    subscriber_hash = hashlib.md5(email_address.lower().encode()).hexdigest()
    result = _delete(f"/lists/{list_id}/members/{subscriber_hash}")
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_delete_member_permanently(list_id: str, email_address: str) -> str:
    """
    Permanently delete a member from an audience. This removes all stored data
    including activity history. This action cannot be undone.

    Args:
        list_id: The unique ID of the audience.
        email_address: The email address of the member to permanently delete.

    Returns:
        JSON string confirming permanent deletion.
    """
    import hashlib
    subscriber_hash = hashlib.md5(email_address.lower().encode()).hexdigest()
    result = _post(
        f"/lists/{list_id}/members/{subscriber_hash}/actions/delete-permanent"
    )
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_batch_subscribe_members(
    list_id: str,
    members: List[dict],
    update_existing: bool = True,
) -> str:
    """
    Add or update multiple members in an audience in a single request (batch upsert).

    Each member dict should contain:
        - email_address (required): The member's email.
        - status (required): "subscribed", "unsubscribed", "cleaned", or "pending".
        - merge_fields (optional): Dict of merge field values (FNAME, LNAME, etc).
        - tags (optional): List of tag name strings.
        - vip (optional): Boolean VIP flag.

    Args:
        list_id: The unique ID of the audience.
        members: List of member dicts, each with at minimum email_address and status.
        update_existing: If True (default), update existing members. If False,
            skip members that already exist.

    Returns:
        JSON string with batch results including new_members count,
        updated_members count, and any errors.
    """
    payload = {
        "members": members,
        "update_existing": update_existing,
    }
    result = _post(f"/lists/{list_id}", payload)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Member Tags
# ---------------------------------------------------------------------------

@mcp.tool()
def mailchimp_get_member_tags(list_id: str, email_address: str) -> str:
    """
    Get all tags applied to a specific member.

    Args:
        list_id: The unique ID of the audience.
        email_address: The member's email address.

    Returns:
        JSON string with list of tag objects (id, name, date_added).
    """
    import hashlib
    subscriber_hash = hashlib.md5(email_address.lower().encode()).hexdigest()
    result = _get(f"/lists/{list_id}/members/{subscriber_hash}/tags")
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_update_member_tags(
    list_id: str,
    email_address: str,
    tags_to_add: Optional[List[str]] = None,
    tags_to_remove: Optional[List[str]] = None,
) -> str:
    """
    Add or remove tags from a specific member.

    Args:
        list_id: The unique ID of the audience.
        email_address: The member's email address.
        tags_to_add: List of tag names to add to the member. Optional.
        tags_to_remove: List of tag names to remove from the member. Optional.

    Returns:
        JSON string confirming the tags were updated.
    """
    import hashlib
    subscriber_hash = hashlib.md5(email_address.lower().encode()).hexdigest()

    tags = []
    if tags_to_add:
        for name in tags_to_add:
            tags.append({"name": name, "status": "active"})
    if tags_to_remove:
        for name in tags_to_remove:
            tags.append({"name": name, "status": "inactive"})

    if not tags:
        raise RuntimeError("Provide at least one tag to add or remove.")

    result = _post(
        f"/lists/{list_id}/members/{subscriber_hash}/tags",
        {"tags": tags}
    )
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Audience Segments
# ---------------------------------------------------------------------------

@mcp.tool()
def mailchimp_list_segments(
    list_id: str,
    count: Optional[int] = None,
    offset: Optional[int] = None,
    type: Optional[str] = None,
) -> str:
    """
    List all segments in an audience.

    Args:
        list_id: The unique ID of the audience.
        count: Number of records to return. Default 10.
        offset: Number of records to skip for pagination.
        type: Filter by segment type: "saved", "static", or "fuzzy". Optional.

    Returns:
        JSON string with array of segment objects.
    """
    params: dict = {}
    if count is not None:
        params["count"] = count
    if offset is not None:
        params["offset"] = offset
    if type:
        params["type"] = type
    result = _get(f"/lists/{list_id}/segments", params=params or None)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_get_segment(list_id: str, segment_id: str) -> str:
    """
    Get details of a specific segment in an audience.

    Args:
        list_id: The unique ID of the audience.
        segment_id: The unique ID of the segment.

    Returns:
        JSON string with segment details including conditions and member count.
    """
    result = _get(f"/lists/{list_id}/segments/{segment_id}")
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

@mcp.tool()
def mailchimp_list_campaigns(
    count: Optional[int] = None,
    offset: Optional[int] = None,
    status: Optional[str] = None,
    type: Optional[str] = None,
    list_id: Optional[str] = None,
    before_send_time: Optional[str] = None,
    since_send_time: Optional[str] = None,
) -> str:
    """
    List campaigns in the Mailchimp account (one page).

    IMPORTANT — For date-based performance reporting, use
    mailchimp_get_performance_report_by_date instead. It auto-paginates,
    never scopes by list_id, and correctly maps all metric fields.
    Passing list_id here will silently exclude campaigns sent to other
    audiences — causing missing campaigns in multi-audience sends.

    Args:
        count: Number of records to return (max 1000). Default 1000.
        offset: Number of records to skip for pagination. Default 0.
        status: Filter by campaign status: "save", "paused", "schedule",
            "sending", "sent", or "canceled". Optional.
        type: Filter by campaign type: "regular", "plaintext", "absplit",
            "rss", or "variate". Optional.
        list_id: Filter by a specific audience ID. OMIT THIS for reporting
            across all audiences — otherwise campaigns sent to other lists
            will be missing from results.
        before_send_time: Filter campaigns sent before this date
            (ISO 8601 format, e.g. "2024-04-15T00:00:00Z"). Optional.
        since_send_time: Filter campaigns sent after this date
            (ISO 8601 format, e.g. "2024-04-14T00:00:00Z"). Optional.

    Returns:
        JSON string with array of campaign objects and total_items count.
    """
    params: dict = {}
    params["count"] = min(count, 1000) if count is not None else 1000
    if offset is not None:
        params["offset"] = offset
    if status:
        params["status"] = status
    if type:
        params["type"] = type
    if list_id:
        params["list_id"] = list_id
    if before_send_time:
        params["before_send_time"] = before_send_time
    if since_send_time:
        params["since_send_time"] = since_send_time
    result = _get("/campaigns", params=params)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_get_campaign(campaign_id: str) -> str:
    """
    Get details of a specific campaign by its ID.

    Args:
        campaign_id: The unique ID of the campaign.

    Returns:
        JSON string with campaign details including settings, recipients,
        tracking options, and status.
    """
    result = _get(f"/campaigns/{campaign_id}")
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_create_campaign(
    type: str,
    list_id: str,
    subject_line: str,
    from_name: str,
    reply_to: str,
    title: Optional[str] = None,
    preview_text: Optional[str] = None,
    template_id: Optional[int] = None,
    segment_id: Optional[int] = None,
    track_opens: bool = True,
    track_clicks: bool = True,
    auto_footer: bool = False,
    inline_css: bool = False,
) -> str:
    """
    Create a new campaign.

    Args:
        type: Campaign type: "regular", "plaintext", "absplit", "rss",
            or "variate".
        list_id: The unique ID of the audience to send to.
        subject_line: The subject line for the campaign email.
        from_name: The sender's name shown in the From field.
        reply_to: The reply-to email address for the campaign.
        title: An internal title for the campaign (not shown to recipients).
            Defaults to subject_line if not provided.
        preview_text: The preview text shown in email clients below the
            subject line. Optional.
        template_id: The ID of a Mailchimp template to use. Optional.
        segment_id: The ID of a segment to narrow the audience. Optional.
        track_opens: Enable open tracking. Default True.
        track_clicks: Enable click tracking. Default True.
        auto_footer: Automatically add Mailchimp footer. Default False.
        inline_css: Automatically inline CSS styles. Default False.

    Returns:
        JSON string with the newly created campaign object including its id.
    """
    settings: dict = {
        "subject_line": subject_line,
        "from_name": from_name,
        "reply_to": reply_to,
        "auto_footer": auto_footer,
        "inline_css": inline_css,
    }
    if title:
        settings["title"] = title
    if preview_text:
        settings["preview_text"] = preview_text
    if template_id is not None:
        settings["template_id"] = template_id

    recipients: dict = {"list_id": list_id}
    if segment_id is not None:
        recipients["segment_opts"] = {"saved_segment_id": segment_id}

    tracking = {
        "opens": track_opens,
        "html_clicks": track_clicks,
        "text_clicks": track_clicks,
    }

    payload: dict = {
        "type": type,
        "recipients": recipients,
        "settings": settings,
        "tracking": tracking,
    }
    result = _post("/campaigns", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_update_campaign(
    campaign_id: str,
    subject_line: Optional[str] = None,
    from_name: Optional[str] = None,
    reply_to: Optional[str] = None,
    title: Optional[str] = None,
    preview_text: Optional[str] = None,
    list_id: Optional[str] = None,
    segment_id: Optional[int] = None,
) -> str:
    """
    Update settings of an existing draft campaign.

    Args:
        campaign_id: The unique ID of the campaign to update.
        subject_line: New subject line. Optional.
        from_name: New sender name. Optional.
        reply_to: New reply-to email. Optional.
        title: New internal title. Optional.
        preview_text: New preview text. Optional.
        list_id: New audience list ID. Optional.
        segment_id: New segment ID to target a sub-section of the audience. Optional.

    Returns:
        JSON string with the updated campaign object.
    """
    payload: dict = {}
    settings: dict = {}
    if subject_line is not None:
        settings["subject_line"] = subject_line
    if from_name is not None:
        settings["from_name"] = from_name
    if reply_to is not None:
        settings["reply_to"] = reply_to
    if title is not None:
        settings["title"] = title
    if preview_text is not None:
        settings["preview_text"] = preview_text
    if settings:
        payload["settings"] = settings

    recipients: dict = {}
    if list_id is not None:
        recipients["list_id"] = list_id
    if segment_id is not None:
        recipients["segment_opts"] = {"saved_segment_id": segment_id}
    if recipients:
        payload["recipients"] = recipients

    if not payload:
        raise RuntimeError("No fields provided to update.")

    result = _patch(f"/campaigns/{campaign_id}", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_set_campaign_content(
    campaign_id: str,
    html: Optional[str] = None,
    plain_text: Optional[str] = None,
    template_id: Optional[int] = None,
    template_sections: Optional[dict] = None,
    url: Optional[str] = None,
) -> str:
    """
    Set the content of a campaign. You can provide HTML, plain text, a
    template reference, or a URL to import content from.

    Args:
        campaign_id: The unique ID of the campaign.
        html: Full HTML content for the campaign email. Optional.
        plain_text: Plain text version of the email content. Optional.
        template_id: ID of a Mailchimp template to use as a base. Optional.
        template_sections: Dict of section content to populate within the
            template (e.g. {"body": "<p>Hello!</p>"}). Optional.
        url: URL to import HTML content from. Optional.

    Returns:
        JSON string with the updated campaign content.
    """
    payload: dict = {}
    if html is not None:
        payload["html"] = html
    if plain_text is not None:
        payload["plain_text"] = plain_text
    if url is not None:
        payload["url"] = url
    if template_id is not None:
        template_ref: dict = {"id": template_id}
        if template_sections:
            template_ref["sections"] = template_sections
        payload["template"] = template_ref
    if not payload:
        raise RuntimeError("Provide at least one content source: html, plain_text, template_id, or url.")
    result = _put(f"/campaigns/{campaign_id}/content", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_get_campaign_content(campaign_id: str) -> str:
    """
    Get the HTML and plain-text content of a campaign.

    Args:
        campaign_id: The unique ID of the campaign.

    Returns:
        JSON string with the campaign's HTML and plain text content.
    """
    result = _get(f"/campaigns/{campaign_id}/content")
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_send_campaign(campaign_id: str) -> str:
    """
    Send a campaign immediately. The campaign must be in "save" or "paused"
    status and have valid content and recipients configured.

    Args:
        campaign_id: The unique ID of the campaign to send.

    Returns:
        JSON string confirming the campaign was sent.
    """
    result = _post(f"/campaigns/{campaign_id}/actions/send")
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_schedule_campaign(
    campaign_id: str,
    schedule_time: str,
    timewarp: bool = False,
    batch_delivery: bool = False,
) -> str:
    """
    Schedule a campaign to be sent at a specific time.

    Args:
        campaign_id: The unique ID of the campaign.
        schedule_time: The UTC datetime to send the campaign, in ISO 8601
            format (e.g. "2024-06-15T14:00:00+00:00").
        timewarp: If True, send based on each recipient's local timezone.
            Default False.
        batch_delivery: If True, deliver in batches. Default False.

    Returns:
        JSON string confirming the campaign was scheduled.
    """
    payload: dict = {
        "schedule_time": schedule_time,
        "timewarp": timewarp,
        "batch_delivery": {"batch_delivery": batch_delivery},
    }
    result = _post(f"/campaigns/{campaign_id}/actions/schedule", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_unschedule_campaign(campaign_id: str) -> str:
    """
    Unschedule a scheduled campaign, moving it back to draft status.

    Args:
        campaign_id: The unique ID of the campaign to unschedule.

    Returns:
        JSON string confirming the campaign was unscheduled.
    """
    result = _post(f"/campaigns/{campaign_id}/actions/unschedule")
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_cancel_campaign(campaign_id: str) -> str:
    """
    Cancel a campaign that is currently sending.

    Args:
        campaign_id: The unique ID of the campaign to cancel.

    Returns:
        JSON string confirming the campaign was canceled.
    """
    result = _post(f"/campaigns/{campaign_id}/actions/cancel-send")
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_send_test_email(
    campaign_id: str,
    test_emails: List[str],
    send_type: str = "html",
) -> str:
    """
    Send a test email for a campaign to specified addresses.

    Args:
        campaign_id: The unique ID of the campaign.
        test_emails: List of email addresses to send the test to.
        send_type: Type of test email to send: "html" or "plaintext".
            Default "html".

    Returns:
        JSON string confirming the test email was sent.
    """
    payload = {
        "test_emails": test_emails,
        "send_type": send_type,
    }
    result = _post(f"/campaigns/{campaign_id}/actions/test", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_replicate_campaign(campaign_id: str) -> str:
    """
    Create a copy (replica) of an existing campaign as a new draft.

    Args:
        campaign_id: The unique ID of the campaign to replicate.

    Returns:
        JSON string with the new replicated campaign object including its id.
    """
    result = _post(f"/campaigns/{campaign_id}/actions/replicate")
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_delete_campaign(campaign_id: str) -> str:
    """
    Permanently delete a campaign. Sent campaigns cannot be deleted.

    Args:
        campaign_id: The unique ID of the campaign to delete.

    Returns:
        JSON string confirming deletion.
    """
    result = _delete(f"/campaigns/{campaign_id}")
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

@mcp.tool()
def mailchimp_list_templates(
    count: Optional[int] = None,
    offset: Optional[int] = None,
    type: Optional[str] = None,
    category: Optional[str] = None,
) -> str:
    """
    List all templates available in the account.

    Args:
        count: Number of records to return. Default 10.
        offset: Number of records to skip for pagination.
        type: Filter by template type: "user", "base", or "gallery". Optional.
        category: Filter by template category name. Optional.

    Returns:
        JSON string with array of template objects including id and name.
    """
    params: dict = {}
    if count is not None:
        params["count"] = count
    if offset is not None:
        params["offset"] = offset
    if type:
        params["type"] = type
    if category:
        params["category"] = category
    result = _get("/templates", params=params or None)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_get_template(template_id: str) -> str:
    """
    Get details of a specific template by its ID.

    Args:
        template_id: The unique ID of the template.

    Returns:
        JSON string with template details including name, type, and HTML content.
    """
    result = _get(f"/templates/{template_id}")
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_create_template(
    name: str,
    html: str,
) -> str:
    """
    Create a new custom HTML template.

    Args:
        name: A name for the template.
        html: The full HTML content of the template. Should use Mailchimp's
            template language for editable sections (mc:edit attributes).

    Returns:
        JSON string with the newly created template object including its id.
    """
    payload = {"name": name, "html": html}
    result = _post("/templates", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_update_template(
    template_id: str,
    name: Optional[str] = None,
    html: Optional[str] = None,
) -> str:
    """
    Update an existing custom template.

    Args:
        template_id: The unique ID of the template to update.
        name: New name for the template. Optional.
        html: New HTML content for the template. Optional.

    Returns:
        JSON string with the updated template object.
    """
    payload: dict = {}
    if name is not None:
        payload["name"] = name
    if html is not None:
        payload["html"] = html
    if not payload:
        raise RuntimeError("Provide at least one field to update: name or html.")
    result = _patch(f"/templates/{template_id}", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_delete_template(template_id: str) -> str:
    """
    Delete a template permanently.

    Args:
        template_id: The unique ID of the template to delete.

    Returns:
        JSON string confirming deletion.
    """
    result = _delete(f"/templates/{template_id}")
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@mcp.tool()
def mailchimp_list_reports(
    count: Optional[int] = None,
    offset: Optional[int] = None,
    type: Optional[str] = None,
    before_send_time: Optional[str] = None,
    since_send_time: Optional[str] = None,
) -> str:
    """
    List campaign reports for the account.

    Args:
        count: Number of records to return. Default 10.
        offset: Number of records to skip for pagination.
        type: Filter by campaign type: "regular", "plaintext", "absplit",
            "rss", or "variate". Optional.
        before_send_time: Return campaigns sent before this date (ISO 8601). Optional.
        since_send_time: Return campaigns sent after this date (ISO 8601). Optional.

    Returns:
        JSON string with array of report objects.
    """
    params: dict = {}
    if count is not None:
        params["count"] = count
    if offset is not None:
        params["offset"] = offset
    if type:
        params["type"] = type
    if before_send_time:
        params["before_send_time"] = before_send_time
    if since_send_time:
        params["since_send_time"] = since_send_time
    result = _get("/reports", params=params or None)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_get_report(campaign_id: str) -> str:
    """
    Get the report for a specific sent campaign, including opens, clicks,
    bounces, unsubscribes, and other key metrics.

    Args:
        campaign_id: The unique ID of the sent campaign.

    Returns:
        JSON string with the full campaign report.
    """
    result = _get(f"/reports/{campaign_id}")
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_get_campaign_open_details(
    campaign_id: str,
    count: Optional[int] = None,
    offset: Optional[int] = None,
) -> str:
    """
    Get a list of members who opened a specific campaign, with open timestamps.

    Args:
        campaign_id: The unique ID of the sent campaign.
        count: Number of records to return. Default 10.
        offset: Number of records to skip for pagination.

    Returns:
        JSON string with list of members who opened the campaign.
    """
    params: dict = {}
    if count is not None:
        params["count"] = count
    if offset is not None:
        params["offset"] = offset
    result = _get(f"/reports/{campaign_id}/open-details", params=params or None)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_get_campaign_click_details(
    campaign_id: str,
    count: Optional[int] = None,
    offset: Optional[int] = None,
) -> str:
    """
    Get click details for all tracked URLs in a sent campaign.

    Args:
        campaign_id: The unique ID of the sent campaign.
        count: Number of records to return. Default 10.
        offset: Number of records to skip for pagination.

    Returns:
        JSON string with click stats per URL including click counts.
    """
    params: dict = {}
    if count is not None:
        params["count"] = count
    if offset is not None:
        params["offset"] = offset
    result = _get(f"/reports/{campaign_id}/click-details", params=params or None)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_get_unsubscribes(
    campaign_id: str,
    count: Optional[int] = None,
    offset: Optional[int] = None,
) -> str:
    """
    Get a list of members who unsubscribed from a specific campaign.

    Args:
        campaign_id: The unique ID of the sent campaign.
        count: Number of records to return. Default 10.
        offset: Number of records to skip for pagination.

    Returns:
        JSON string with list of unsubscribed members.
    """
    params: dict = {}
    if count is not None:
        params["count"] = count
    if offset is not None:
        params["offset"] = offset
    result = _get(f"/reports/{campaign_id}/unsubscribed", params=params or None)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_get_email_activity(
    campaign_id: str,
    count: Optional[int] = None,
    offset: Optional[int] = None,
) -> str:
    """
    Get per-recipient email activity for a sent campaign (opens, clicks,
    bounces per email address).

    Args:
        campaign_id: The unique ID of the sent campaign.
        count: Number of records to return. Default 10.
        offset: Number of records to skip for pagination.

    Returns:
        JSON string with per-email activity breakdown.
    """
    params: dict = {}
    if count is not None:
        params["count"] = count
    if offset is not None:
        params["offset"] = offset
    result = _get(f"/reports/{campaign_id}/email-activity", params=params or None)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_get_bot_filtered_clicker_contacts(
    campaign_id: str,
    include_bots: bool = False,
) -> str:
    """
    Return the list of contacts who clicked links in a campaign, with bot-filtering applied.

    Uses timing analysis on the email-activity endpoint: subscribers who clicked more than
    one link and had ALL clicks occur within 2 seconds of each other are classified as bots
    (security-scanner behaviour). Everyone else is classified as a human clicker.

    Args:
        campaign_id: The unique ID of the sent campaign (e.g. "0a7340ab2c").
        include_bots: If False (default), returns only human/legitimate clickers.
                      If True, returns all clickers with their classification.

    Returns:
        JSON string with:
          {
            "campaign_id": str,
            "human_clicker_count": int,
            "bot_clicker_count": int,
            "bot_gap_threshold_seconds": int,
            "contacts": [
              {
                "email_address": str,
                "classification": "human" | "bot",
                "click_count": int,
                "first_click": str,   # ISO8601
                "last_click": str,    # ISO8601
              },
              ...
            ]
          }
    """
    classified = _classify_clickers(campaign_id)

    human_emails = [e for e, v in classified.items() if v["is_human"]]
    bot_emails   = [e for e, v in classified.items() if not v["is_human"]]

    contacts = []
    for email, info in sorted(classified.items()):
        if not include_bots and not info["is_human"]:
            continue
        contacts.append({
            "email_address": email,
            "classification": "human" if info["is_human"] else "bot",
            "click_count": info["click_count"],
            "first_click": info["timestamps"][0] if info["timestamps"] else None,
            "last_click":  info["timestamps"][-1] if info["timestamps"] else None,
        })

    return json.dumps({
        "campaign_id": campaign_id,
        "human_clicker_count": len(human_emails),
        "bot_clicker_count": len(bot_emails),
        "bot_gap_threshold_seconds": BOT_CLICK_GAP_SECONDS,
        "contacts": contacts,
    }, indent=2)


# ---------------------------------------------------------------------------
# Automations (Classic)
# ---------------------------------------------------------------------------

@mcp.tool()
def mailchimp_list_automations(
    count: Optional[int] = None,
    offset: Optional[int] = None,
    status: Optional[str] = None,
) -> str:
    """
    List all classic automations (auto-responders) in the account.

    Args:
        count: Number of records to return. Default 10.
        offset: Number of records to skip for pagination.
        status: Filter by status: "save", "paused", or "sending". Optional.

    Returns:
        JSON string with array of automation workflow objects.
    """
    params: dict = {}
    if count is not None:
        params["count"] = count
    if offset is not None:
        params["offset"] = offset
    if status:
        params["status"] = status
    result = _get("/automations", params=params or None)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_get_automation(workflow_id: str) -> str:
    """
    Get details of a specific classic automation workflow.

    Args:
        workflow_id: The unique ID of the automation workflow.

    Returns:
        JSON string with automation details including emails, status, and stats.
    """
    result = _get(f"/automations/{workflow_id}")
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_pause_automation(workflow_id: str) -> str:
    """
    Pause all emails in an active classic automation workflow.

    Args:
        workflow_id: The unique ID of the automation workflow to pause.

    Returns:
        JSON string confirming the automation was paused.
    """
    result = _post(f"/automations/{workflow_id}/actions/pause-all-emails")
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_start_automation(workflow_id: str) -> str:
    """
    Start or resume all emails in a classic automation workflow.

    Args:
        workflow_id: The unique ID of the automation workflow to start.

    Returns:
        JSON string confirming the automation was started.
    """
    result = _post(f"/automations/{workflow_id}/actions/start-all-emails")
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_add_subscriber_to_automation(
    workflow_id: str,
    email_address: str,
) -> str:
    """
    Add a subscriber to the start of a classic automation workflow.

    Args:
        workflow_id: The unique ID of the automation workflow.
        email_address: The email address of the subscriber to add.

    Returns:
        JSON string confirming the subscriber was added to the automation.
    """
    result = _post(
        f"/automations/{workflow_id}/emails/{workflow_id}/queue",
        {"email_address": email_address}
    )
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@mcp.tool()
def mailchimp_search_members(
    query: str,
    list_id: Optional[str] = None,
) -> str:
    """
    Search for members across all audiences by email address or name.

    Args:
        query: The search query string (email address or name fragment).
        list_id: Limit results to a specific audience. Optional.

    Returns:
        JSON string with matching members across exact matches and
        full-text search results.
    """
    params: dict = {"query": query}
    if list_id:
        params["list_id"] = list_id
    result = _get("/search-members", params=params)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_search_campaigns(query: str) -> str:
    """
    Search for campaigns by keyword.

    Args:
        query: The search keyword to match against campaign names, subjects,
            and content.

    Returns:
        JSON string with matching campaigns and snippets.
    """
    result = _get("/search-campaigns", params={"query": query})
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Account-wide Campaign Fetch (no list_id scoping)
# ---------------------------------------------------------------------------

@mcp.tool()
def mailchimp_get_all_sent_campaigns(
    since_send_time: str,
    before_send_time: str,
) -> dict:
    """
    Retrieve ALL sent campaigns account-wide within a date window, auto-paginating
    through every page so no campaigns are missed. Never scopes by list_id, so
    campaigns sent to different audiences (APAC, EMEA, US, etc.) are all included.

    Use this — not mailchimp_list_campaigns — whenever you need a complete list of
    campaigns for a given day or date range across all audiences.

    Only returns the essential fields per campaign (id, title, subject_line,
    send_time, emails_sent, audience, report_summary) — NOT the raw API firehose
    (no _links, segment HTML, tracking settings, variate_settings, etc.).

    Args:
        since_send_time: Return campaigns sent after this datetime
            (ISO 8601, e.g. "2024-04-14T00:00:00Z").
        before_send_time: Return campaigns sent before this datetime
            (ISO 8601, e.g. "2024-04-15T00:00:00Z").

    Returns:
        Dict with total_count and slim campaigns list (all pages combined).
    """
    _SLIM_FIELDS = (
        "campaigns.id,campaigns.settings.title,campaigns.settings.subject_line,"
        "campaigns.send_time,campaigns.emails_sent,campaigns.recipients.list_name,"
        "campaigns.report_summary,total_items"
    )
    all_campaigns = []
    offset = 0
    page_size = 1000
    while True:
        params = {
            "status": "sent",
            "since_send_time": since_send_time,
            "before_send_time": before_send_time,
            "count": page_size,
            "offset": offset,
            "fields": _SLIM_FIELDS,
        }
        data = _get("/campaigns", params=params)
        if not data or "campaigns" not in data:
            break
        page = data.get("campaigns", [])
        all_campaigns.extend(page)
        total = data.get("total_items", 0)
        if len(all_campaigns) >= total or len(page) < page_size:
            break
        offset += page_size

    slim = []
    for c in all_campaigns:
        s = c.get("settings", {})
        rs = c.get("report_summary", {})
        slim.append({
            "id":           c.get("id", ""),
            "title":        s.get("title", ""),
            "subject_line": s.get("subject_line", ""),
            "send_time":    c.get("send_time", ""),
            "emails_sent":  c.get("emails_sent", 0),
            "audience":     c.get("recipients", {}).get("list_name", ""),
            "report_summary": {
                "unique_opens":      rs.get("unique_opens", 0),
                "open_rate":         rs.get("open_rate", 0),
                "subscriber_clicks": rs.get("subscriber_clicks", 0),
                "click_rate":        rs.get("click_rate", 0),
            },
        })

    return {
        "total_count": len(slim),
        "since_send_time": since_send_time,
        "before_send_time": before_send_time,
        "campaigns": slim,
    }


# ---------------------------------------------------------------------------
# Performance Report by Date (correct field mapping + account-wide)
# ---------------------------------------------------------------------------

@mcp.tool()
def mailchimp_get_performance_report_by_date(
    since_send_time: str,
    before_send_time: str,
    include_per_campaign_reports: bool = True,
    exclude_bot_activity: bool = True,
) -> dict:
    """
    DEPRECATED FOR MULTI-DAY RANGES — use mailchimp_reports_performance_board instead.
    This tool makes one sequential HTTP call per campaign and reliably times out on
    windows wider than ~1 day (50+ campaigns). It exists only for single-day or
    narrow windows where the campaign count is known to be small (<20 campaigns).

    For ANY request spanning 2 or more days, call mailchimp_reports_performance_board
    which pages the /reports endpoint directly — all metrics in one call, no timeouts.

    Legacy field mapping reference (single-day use only):
    1. Fetches every sent campaign account-wide (no list_id scoping) so campaigns
       sent to different audiences (APAC, EMEA, US, USMM, ENT etc.) are never missed.
    2. Auto-paginates so campaigns beyond the first page are included.
    3. Uses the correct Mailchimp field mappings:
       - unique_clickers  → clicks.unique_subscriber_clicks  (recipients who clicked)
       - total_clicks     → clicks.clicks_total
       - unique_opens     → opens.unique_opens
       - total_opens      → opens.opens_total
       - emails_sent      → emails_sent
       - bounces          → bounces.hard_bounces + bounces.soft_bounces
       - unsubscribes     → unsubscribed
    NOTE: unique_clickers (unique_subscriber_clicks) is the per-person click count
    that matches the Mailchimp UI export. Do NOT use unique_clicks for this —
    that field counts per-URL clicks across all clickers, not unique people.

    Args:
        since_send_time: Start of the window (ISO 8601, e.g. "2024-04-14T00:00:00Z").
        before_send_time: End of the window (ISO 8601, e.g. "2024-04-15T00:00:00Z").
        include_per_campaign_reports: If True (default), fetch detailed report for
            each campaign and include correct metric fields. If False, return only
            campaign list metadata (faster but less detail).
        exclude_bot_activity: When True (default), uses bot-excluded metrics:
            - Opens  → proxy_excluded_unique_opens / proxy_excluded_open_rate
              (removes Apple MPP and machine-generated opens)
            - Clicks → timing-based analysis via email-activity; subscribers who clicked
              multiple links within 2 seconds are excluded as security-scanner bots.
              This approximates Mailchimp's "Bot filtering ON" click count in the UI.
            Set False only if you explicitly need raw unfiltered metrics.

    Returns:
        Dict with summary totals and per-campaign breakdown with correct metrics.
        When exclude_bot_activity=True, includes "bot_filtering": "enabled" in output.
    """
    all_campaigns = []
    offset = 0
    page_size = 1000
    while True:
        params = {
            "status": "sent",
            "since_send_time": since_send_time,
            "before_send_time": before_send_time,
            "count": page_size,
            "offset": offset,
        }
        data = _get("/campaigns", params=params)
        if not data or "campaigns" not in data:
            break
        page = data.get("campaigns", [])
        all_campaigns.extend(page)
        total = data.get("total_items", 0)
        if len(all_campaigns) >= total or len(page) < page_size:
            break
        offset += page_size

    campaign_count = len(all_campaigns)
    reports = []

    for camp in all_campaigns:
        cid = camp.get("id", "")
        name = camp.get("settings", {}).get("title") or camp.get("settings", {}).get("subject_line", "")
        send_time = camp.get("send_time", "")
        list_name = camp.get("recipients", {}).get("list_name", "")

        row: dict = {
            "campaign_id": cid,
            "campaign_name": name,
            "send_time": send_time,
            "audience": list_name,
        }

        if include_per_campaign_reports and cid:
            rdata = _get(f"/reports/{cid}")
            if rdata and "error" not in rdata:
                opens = rdata.get("opens", {})
                clicks = rdata.get("clicks", {})
                bounces = rdata.get("bounces", {})
                sent = rdata.get("emails_sent", 0)
                hb = bounces.get("hard_bounces", 0)
                sb = bounces.get("soft_bounces", 0)
                delivered = sent - hb - sb

                if exclude_bot_activity:
                    u_opens  = opens.get("proxy_excluded_unique_opens", opens.get("unique_opens", 0))
                    t_opens  = opens.get("proxy_excluded_opens", opens.get("opens_total", 0))
                    o_rate   = opens.get("proxy_excluded_open_rate", opens.get("open_rate", 0))
                    raw_u_clicks = clicks.get("unique_subscriber_clicks", 0)
                    u_clicks = _bot_filtered_clickers(cid, raw_u_clicks)
                    t_clicks = clicks.get("clicks_total", 0)
                    c_rate   = (u_clicks / delivered) if delivered else 0
                else:
                    u_opens  = opens.get("unique_opens", 0)
                    t_opens  = opens.get("opens_total", 0)
                    o_rate   = opens.get("open_rate", 0)
                    u_clicks = clicks.get("unique_subscriber_clicks", 0)
                    t_clicks = clicks.get("clicks_total", 0)
                    c_rate   = clicks.get("click_rate", 0)

                row.update({
                    "emails_sent": sent,
                    "emails_delivered": delivered,
                    "unique_opens": u_opens,
                    "total_opens": t_opens,
                    "open_rate": o_rate,
                    "unique_clickers": u_clicks,
                    "total_clicks": t_clicks,
                    "click_rate": c_rate,
                    "hard_bounces": hb,
                    "soft_bounces": sb,
                    "total_bounces": hb + sb,
                    "unsubscribes": rdata.get("unsubscribed", 0),
                    "abuse_reports": rdata.get("abuse_reports", 0),
                })
            else:
                row["report_error"] = rdata.get("error", "report not available") if rdata else "report not available"
        else:
            summary = camp.get("report_summary", {})
            row.update({
                "emails_sent": camp.get("emails_sent", 0),
                "unique_opens": summary.get("unique_opens", 0),
                "open_rate": summary.get("open_rate", 0),
                "unique_clickers": summary.get("subscriber_clicks", 0),
                "total_clicks": summary.get("clicks", 0),
                "click_rate": summary.get("click_rate", 0),
            })

        reports.append(row)

    totals: dict = {
        "campaign_count": campaign_count,
        "total_sent": sum(r.get("emails_sent", 0) for r in reports),
        "total_delivered": sum(r.get("emails_delivered", 0) for r in reports),
        "total_unique_opens": sum(r.get("unique_opens", 0) for r in reports),
        "total_unique_clickers": sum(r.get("unique_clickers", 0) for r in reports),
        "total_bounces": sum(r.get("total_bounces", 0) for r in reports),
        "total_unsubscribes": sum(r.get("unsubscribes", 0) for r in reports),
    }

    return {
        "since_send_time": since_send_time,
        "before_send_time": before_send_time,
        "bot_filtering": "enabled (proxy_excluded metrics)" if exclude_bot_activity else "disabled (standard metrics)",
        "summary": totals,
        "campaigns": reports,
    }


# ---------------------------------------------------------------------------
# Batch Operations helper — uses /batches to fetch up to 500 reports at once
# ---------------------------------------------------------------------------

def _batch_get_reports(campaign_ids: list, poll_interval: float = 3.0,
                       max_wait: float = 180.0) -> dict:
    """
    Fetch /reports/{cid} for every campaign_id in ONE Mailchimp Batch call.

    Mailchimp's /batches endpoint queues up to 500 GET operations, processes
    them server-side, and returns all results as a tar.gz download.  This is
    dramatically faster than 150+ sequential (or even concurrent) HTTP calls.

    Returns:
        dict  campaign_id → report_data_dict (empty dict on per-campaign error)
        None  if the batch submission or poll fails fatally.
    """
    import io, tarfile, re as _re

    if not campaign_ids:
        return {}

    results: dict = {}
    # Split into chunks of 500 (Mailchimp batch limit)
    chunk_size = 500
    chunks = [campaign_ids[i:i+chunk_size] for i in range(0, len(campaign_ids), chunk_size)]

    for chunk in chunks:
        operations = [
            {
                "method": "GET",
                "path": f"/3.0/reports/{cid}",
                "operation_id": cid,
            }
            for cid in chunk
        ]

        # Submit batch
        try:
            resp = _post("/batches", {"operations": operations})
        except Exception as exc:
            print(f"[_batch_get_reports] batch submit failed: {exc}")
            return None

        batch_id = resp.get("id", "")
        if not batch_id:
            print(f"[_batch_get_reports] no batch_id in response: {resp}")
            return None

        # Poll until finished
        deadline = time.monotonic() + max_wait
        status = ""
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            try:
                poll = _get(f"/batches/{batch_id}")
            except Exception:
                continue
            status = poll.get("status", "")
            if status == "finished":
                response_body_url = poll.get("response_body_url", "")
                break
            if status in ("cancelled", "failed"):
                print(f"[_batch_get_reports] batch {batch_id} ended with status={status}")
                return None
        else:
            print(f"[_batch_get_reports] batch {batch_id} timed out (status={status})")
            return None

        # Download tar.gz
        if not response_body_url:
            continue
        try:
            dl = httpx.get(response_body_url, timeout=60, follow_redirects=True)
            dl.raise_for_status()
        except Exception as exc:
            print(f"[_batch_get_reports] download failed: {exc}")
            return None

        # Extract and parse each response file
        try:
            with tarfile.open(fileobj=io.BytesIO(dl.content), mode="r:gz") as tar:
                for member in tar.getmembers():
                    f = tar.extractfile(member)
                    if not f:
                        continue
                    raw = f.read().decode("utf-8", errors="replace")
                    # Each file is a JSON object with: operation_id, status_code, response
                    try:
                        entry = json.loads(raw)
                    except Exception:
                        continue
                    cid = entry.get("operation_id", "")
                    if not cid:
                        # Try to extract from filename: "<batch_id>-<cid>-response.json"
                        m = _re.search(r"-([a-f0-9]+)-response", member.name)
                        if m:
                            cid = m.group(1)
                    if not cid:
                        continue
                    status_code = entry.get("status_code", 0)
                    if status_code == 200:
                        try:
                            results[cid] = json.loads(entry.get("response", "{}"))
                        except Exception:
                            results[cid] = {}
                    else:
                        results[cid] = {}
        except Exception as exc:
            print(f"[_batch_get_reports] tar extraction failed: {exc}")
            return None

    return results


# ---------------------------------------------------------------------------
# Campaign Performance Board — multi-day, batch API, CSV output
# ---------------------------------------------------------------------------

@mcp.tool()
def mailchimp_campaign_performance_board(
    since_send_time: str,
    before_send_time: str,
    exclude_bot_activity: bool = True,
) -> str:
    """
    DEPRECATED FOR MULTI-DAY RANGES — use mailchimp_reports_performance_board instead.

    mailchimp_reports_performance_board hits the /reports endpoint directly and
    returns all campaign metrics (opens, clicks, bounces, unsubscribes) in one
    paginated call — no N+1 per-campaign fetches, no batch polling, and no
    timeout risk for windows up to 20 days.

    This tool (mailchimp_campaign_performance_board) is kept only as a fallback.
    It fetches the campaign list first, then fires concurrent /reports/{cid} calls
    which frequently time out for windows larger than 1-2 days.

    Args:
        since_send_time:      Start of window (ISO 8601, e.g. "2026-04-21T00:00:00Z").
        before_send_time:     End of window   (ISO 8601, e.g. "2026-04-30T23:59:59Z").
        exclude_bot_activity: True (default) = proxy-excluded opens + per-person clicks.

    Returns:
        A CSV string with columns:
          date, campaign_name, subject_line, audience,
          emails_sent, delivered, unique_opens, open_rate_pct,
          unique_clickers, click_rate_pct,
          hard_bounces, soft_bounces, unsubscribes, bot_filtering
        First row = header. Last row = TOTAL across all campaigns.
    """
    import io, csv as _csv

    # Intentionally lean — no report_summary (large nested object slows the
    # response dramatically).  emails_sent is a plain integer and safe to include.
    _SLIM_FIELDS = (
        "campaigns.id,campaigns.settings.title,campaigns.settings.subject_line,"
        "campaigns.send_time,campaigns.recipients.list_name,"
        "campaigns.emails_sent,total_items"
    )

    # ── 1. Fetch campaign list — fast pager with 20 s timeout, break on failure ─
    # Each page is small (100 items, lean fields) so it responds in ~2-3 s.
    # If any page times out we stop and use whatever we collected — partial results
    # are better than a full timeout crash.
    all_campaigns: list = []
    pagination_truncated = False
    offset = 0
    page_size = 100  # small pages → small responses → fast per-request latency
    while True:
        data = _fetch_campaign_page({
            "status": "sent",
            "since_send_time": since_send_time,
            "before_send_time": before_send_time,
            "count": page_size,
            "offset": offset,
            "fields": _SLIM_FIELDS,
        })
        if data is None:
            pagination_truncated = True
            break
        page = data.get("campaigns", [])
        all_campaigns.extend(page)
        if len(all_campaigns) >= data.get("total_items", 0) or len(page) < page_size:
            break
        offset += page_size

    if not all_campaigns:
        return (
            f"No sent campaigns found between {since_send_time} and {before_send_time}. "
            "The Mailchimp campaign-list API may be temporarily unresponsive — try again shortly."
        )

    # ── 2. Concurrently fetch full reports for bot-filtered opens + bounces ──
    # _fetch_report enriches the baseline metrics from step 1 with:
    #   - proxy_excluded_unique_opens / proxy_excluded_open_rate (bot-filtered opens)
    #   - hard_bounces, soft_bounces, unsubscribes
    # Any campaign whose report doesn't return within the budget keeps its
    # baseline metrics from the campaign list (opens/clicks always present).
    from concurrent.futures import ThreadPoolExecutor, wait as _cf_wait

    _BATCH_TIMEOUT = 75  # seconds — hard ceiling for all concurrent fetches

    campaign_ids = [c.get("id", "") for c in all_campaigns if c.get("id")]

    batch_results: dict = {}
    skipped_count = 0
    with ThreadPoolExecutor(max_workers=10) as pool:
        future_map = {pool.submit(_fetch_report, cid): cid for cid in campaign_ids}
        done, not_done = _cf_wait(future_map.keys(), timeout=_BATCH_TIMEOUT)
        for f in not_done:
            f.cancel()
        skipped_count = len(not_done)
        for f in done:
            try:
                cid_r, r = f.result(timeout=0)
                batch_results[cid_r] = r
            except Exception:
                pass

    # ── 3. Build rows ─────────────────────────────────────────────────────
    # Full report (rdata) → enriched/bot-filtered metrics.
    # If _fetch_report timed out (rdata == {}), fall back to:
    #   emails_sent  — from the lean campaign-list field (always present)
    #   all metrics  — zeroed (no data available); marked in bot_filtering column
    rows = []
    for camp in all_campaigns:
        cid  = camp.get("id", "")
        s    = camp.get("settings", {})
        name = s.get("title") or s.get("subject_line", cid)
        subj = s.get("subject_line", "")
        aud  = camp.get("recipients", {}).get("list_name", "")
        st   = camp.get("send_time", "")
        date = st[:10] if st else ""

        b_sent = camp.get("emails_sent", 0) or 0  # lean baseline — always present

        rdata   = batch_results.get(cid, {})
        opens   = rdata.get("opens", {})
        clicks  = rdata.get("clicks", {})
        bounces = rdata.get("bounces", {})
        hb      = bounces.get("hard_bounces", 0)
        sb      = bounces.get("soft_bounces", 0)
        r_sent  = rdata.get("emails_sent", b_sent) or b_sent
        delivered = max(r_sent - hb - sb, 0)

        if rdata and opens:
            if exclude_bot_activity:
                u_opens = opens.get("proxy_excluded_unique_opens", opens.get("unique_opens", 0))
                o_rate  = round(opens.get("proxy_excluded_open_rate", opens.get("open_rate", 0)) * 100, 2)
            else:
                u_opens = opens.get("unique_opens", 0)
                o_rate  = round(opens.get("open_rate", 0) * 100, 2)
            u_clicks = clicks.get("unique_subscriber_clicks", 0)
            c_rate   = round((u_clicks / delivered * 100) if delivered else 0, 2)
        else:
            # Full report not available — metrics unknown, show zeros
            u_opens = u_clicks = hb = sb = 0
            o_rate = c_rate = 0.0
            delivered = b_sent  # best we have without bounce data

        rows.append({
            "date": date, "campaign_name": name, "subject_line": subj,
            "audience": aud,
            "emails_sent": r_sent,
            "delivered": delivered,
            "unique_opens": u_opens, "open_rate_pct": o_rate,
            "unique_clickers": u_clicks, "click_rate_pct": c_rate,
            "hard_bounces": hb, "soft_bounces": sb,
            "unsubscribes": rdata.get("unsubscribed", 0),
        })

    # Sort by date then campaign name
    rows.sort(key=lambda r: (r["date"], r["campaign_name"]))

    # ── 3. Compute totals ─────────────────────────────────────────────────
    total_sent       = sum(r["emails_sent"]     for r in rows)
    total_delivered  = sum(r["delivered"]        for r in rows)
    total_u_opens    = sum(r["unique_opens"]     for r in rows)
    total_u_clickers = sum(r["unique_clickers"]  for r in rows)
    total_hb         = sum(r["hard_bounces"]     for r in rows)
    total_sb         = sum(r["soft_bounces"]     for r in rows)
    total_unsubs     = sum(r["unsubscribes"]     for r in rows)
    agg_open_rate    = round((total_u_opens    / total_delivered * 100) if total_delivered else 0, 2)
    agg_click_rate   = round((total_u_clickers / total_delivered * 100) if total_delivered else 0, 2)

    # ── 4. Build CSV ──────────────────────────────────────────────────────
    enriched_count = len(batch_results)
    total_count    = len(campaign_ids)

    # bot_filtering label
    if skipped_count == 0 and not pagination_truncated:
        bot_label = "enabled" if exclude_bot_activity else "disabled"
    else:
        parts = []
        if enriched_count < total_count:
            parts.append(f"{enriched_count}/{total_count} reports enriched; "
                         f"{skipped_count} zeroed (report fetch timed out)")
        if pagination_truncated:
            parts.append("campaign list truncated — some pages timed out")
        bot_label = ("partial: " if exclude_bot_activity else "disabled/partial: ") + "; ".join(parts)

    buf = io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow([
        "date", "campaign_name", "subject_line", "audience",
        "emails_sent", "delivered", "unique_opens", "open_rate_pct",
        "unique_clickers", "click_rate_pct",
        "hard_bounces", "soft_bounces", "unsubscribes", "bot_filtering",
    ])
    for r in rows:
        writer.writerow([
            r["date"], r["campaign_name"], r["subject_line"], r["audience"],
            r["emails_sent"], r["delivered"], r["unique_opens"], r["open_rate_pct"],
            r["unique_clickers"], r["click_rate_pct"],
            r["hard_bounces"], r["soft_bounces"], r["unsubscribes"], bot_label,
        ])
    # Totals row
    trunc_note    = " | LIST TRUNCATED (some pages timed out)" if pagination_truncated else ""
    skipped_note  = f" | {skipped_count} report(s) zeroed (timed out)" if skipped_count else ""
    writer.writerow([
        f"{since_send_time[:10]} to {before_send_time[:10]}",
        f"TOTAL ({len(rows)} campaigns{trunc_note}{skipped_note})", "", "",
        total_sent, total_delivered, total_u_opens, agg_open_rate,
        total_u_clickers, agg_click_rate,
        total_hb, total_sb, total_unsubs, bot_label,
    ])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Reports-API performance board (single paginated call — no N+1 fetches)
# ---------------------------------------------------------------------------

@mcp.tool()
def mailchimp_reports_performance_board(
    since_send_time: str,
    before_send_time: str,
    exclude_bot_activity: bool = True,
) -> str:
    """Pull 1–20 days of Mailchimp campaign performance in one efficient call.

    Uses the /reports endpoint which returns opens, clicks, bounces, and
    unsubscribes directly for every campaign in the date range — no per-campaign
    follow-up requests needed.  This makes it far faster and more reliable than
    mailchimp_campaign_performance_board for multi-day windows.

    Use this tool (not mailchimp_campaign_performance_board) for any date range
    spanning more than 1 day.

    Args:
        since_send_time:  ISO-8601 start, e.g. "2026-04-21T00:00:00Z"
        before_send_time: ISO-8601 end,   e.g. "2026-04-30T23:59:59Z"
        exclude_bot_activity: If True, use proxy-excluded (bot-filtered) open
                              rates.  Defaults to True.

    Returns:
        CSV string with columns:
          date, campaign_name, subject_line, audience,
          emails_sent, delivered, unique_opens, open_rate_pct,
          unique_clickers, click_rate_pct,
          hard_bounces, soft_bounces, unsubscribes, bot_filtering
        First row = header.  Last row = TOTAL across all campaigns.
        If pagination was cut short by a timeout, the TOTAL row notes it.
    """
    import io, csv as _csv

    # Request only the fields we actually use — keeps each page small and fast.
    _FIELDS = (
        "reports.id,reports.campaign_title,reports.subject_line,"
        "reports.list_name,reports.send_time,reports.emails_sent,"
        "reports.opens.unique_opens,reports.opens.open_rate,"
        "reports.opens.proxy_excluded_unique_opens,"
        "reports.opens.proxy_excluded_open_rate,"
        "reports.clicks.unique_subscriber_clicks,reports.clicks.click_rate,"
        "reports.bounces.hard_bounces,reports.bounces.soft_bounces,"
        "reports.unsubscribed,total_items"
    )

    # ── 1. Page through /reports — one call per page, 20 s timeout each ───
    all_reports: list = []
    pagination_truncated = False
    offset = 0
    page_size = 100
    while True:
        data = _fetch_reports_page({
            "since_send_time": since_send_time,
            "before_send_time": before_send_time,
            "count": page_size,
            "offset": offset,
            "fields": _FIELDS,
        })
        if data is None:
            pagination_truncated = True
            break
        page = data.get("reports", [])
        all_reports.extend(page)
        if len(all_reports) >= data.get("total_items", 0) or len(page) < page_size:
            break
        offset += page_size

    if not all_reports:
        return (
            f"No sent campaigns found between {since_send_time} and {before_send_time}. "
            "The Mailchimp /reports API may be temporarily unresponsive — try again shortly."
        )

    # ── 2. Build rows ─────────────────────────────────────────────────────
    rows = []
    for rep in all_reports:
        st      = rep.get("send_time", "")
        date    = st[:10] if st else ""
        name    = rep.get("campaign_title", rep.get("id", ""))
        subj    = rep.get("subject_line", "")
        aud     = rep.get("list_name", "")
        sent    = rep.get("emails_sent", 0) or 0

        opens   = rep.get("opens", {})
        clicks  = rep.get("clicks", {})
        bounces = rep.get("bounces", {})
        hb      = bounces.get("hard_bounces", 0)
        sb      = bounces.get("soft_bounces", 0)
        delivered = max(sent - hb - sb, 0)

        if exclude_bot_activity:
            u_opens = opens.get("proxy_excluded_unique_opens",
                                opens.get("unique_opens", 0))
            raw_rate = opens.get("proxy_excluded_open_rate",
                                 opens.get("open_rate", 0)) or 0
        else:
            u_opens  = opens.get("unique_opens", 0)
            raw_rate = opens.get("open_rate", 0) or 0
        o_rate = round(raw_rate * 100, 2)

        u_clicks = clicks.get("unique_subscriber_clicks", 0)
        c_rate   = round((u_clicks / delivered * 100) if delivered else 0, 2)

        rows.append({
            "date": date, "campaign_name": name, "subject_line": subj,
            "audience": aud,
            "emails_sent": sent, "delivered": delivered,
            "unique_opens": u_opens, "open_rate_pct": o_rate,
            "unique_clickers": u_clicks, "click_rate_pct": c_rate,
            "hard_bounces": hb, "soft_bounces": sb,
            "unsubscribes": rep.get("unsubscribed", 0),
        })

    rows.sort(key=lambda r: (r["date"], r["campaign_name"]))

    # ── 3. Totals ─────────────────────────────────────────────────────────
    total_sent       = sum(r["emails_sent"]    for r in rows)
    total_delivered  = sum(r["delivered"]      for r in rows)
    total_u_opens    = sum(r["unique_opens"]   for r in rows)
    total_u_clickers = sum(r["unique_clickers"] for r in rows)
    total_hb         = sum(r["hard_bounces"]   for r in rows)
    total_sb         = sum(r["soft_bounces"]   for r in rows)
    total_unsubs     = sum(r["unsubscribes"]   for r in rows)
    agg_open_rate    = round((total_u_opens    / total_delivered * 100) if total_delivered else 0, 2)
    agg_click_rate   = round((total_u_clickers / total_delivered * 100) if total_delivered else 0, 2)

    # ── 4. CSV ────────────────────────────────────────────────────────────
    bot_label  = "enabled" if exclude_bot_activity else "disabled"
    trunc_note = " | LIST TRUNCATED (some pages timed out)" if pagination_truncated else ""

    buf = io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow([
        "date", "campaign_name", "subject_line", "audience",
        "emails_sent", "delivered", "unique_opens", "open_rate_pct",
        "unique_clickers", "click_rate_pct",
        "hard_bounces", "soft_bounces", "unsubscribes", "bot_filtering",
    ])
    for r in rows:
        writer.writerow([
            r["date"], r["campaign_name"], r["subject_line"], r["audience"],
            r["emails_sent"], r["delivered"], r["unique_opens"], r["open_rate_pct"],
            r["unique_clickers"], r["click_rate_pct"],
            r["hard_bounces"], r["soft_bounces"], r["unsubscribes"], bot_label,
        ])
    writer.writerow([
        f"{since_send_time[:10]} to {before_send_time[:10]}",
        f"TOTAL ({len(rows)} campaigns{trunc_note})", "", "",
        total_sent, total_delivered, total_u_opens, agg_open_rate,
        total_u_clickers, agg_click_rate,
        total_hb, total_sb, total_unsubs, bot_label,
    ])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Full formatted campaign report (Python-rendered markdown, no LLM generation)
# ---------------------------------------------------------------------------

@mcp.tool()
def mailchimp_full_campaign_report(
    target_date: str,
    bounce_rate_threshold: float = 4.0,
    open_rate_threshold: float = 25.0,
    unsubscribe_threshold: int = 3,
    exclude_bot_activity: bool = True,
) -> str:
    """
    Fetch ALL campaigns sent on a specific date, pull their full reports, group them
    by region, and return a COMPLETE, PRE-FORMATTED markdown report with:
      - Per-campaign table with all key metrics (sent, delivered, opens, clicks,
        bounces, unsubscribes, abuse, forwards)
      - Regional subtotals (blended open rate, blended click rate, total bounces, etc.)
      - Grand total row across all regions
      - Anomaly flags (high bounce rate, zero clicks with high opens, low open rate,
        high unsubscribes)

    Region grouping rules (applied to campaign title/name prefix):
      - Starts with "APAC"   → APAC
      - Starts with "EU"     → EU
      - Starts with "USMM"   → USMM
      - Starts with "US_ENT" → US ENT
      - Starts with "CM_APS" → Content Marketing APS
      - Anything else        → Other

    IMPORTANT: Use this tool AS THE FIRST AND ONLY CALL whenever the user asks for
    a campaign performance report, campaign metrics, or campaign analytics for a
    specific date. Do NOT call mailchimp_list_campaigns or mailchimp_get_report
    first — this tool already handles listing, fetching individual reports, grouping,
    subtotals, and anomaly detection internally. Calling any other campaign tool
    before this one wastes time and may cause timeouts.

    Args:
        target_date: The date to report on. Accepts YYYY-MM-DD (e.g. "2026-04-15")
            or ISO 8601 with time (the date part is extracted automatically).
        bounce_rate_threshold: Flag campaigns where bounce rate exceeds this %.
            Default 4.0.
        open_rate_threshold: Flag campaigns where open rate is below this %.
            Default 25.0.
        unsubscribe_threshold: Flag campaigns where unsubscribes >= this number.
            Default 3.
        exclude_bot_activity: When True (default), uses bot-excluded metrics:
            - Opens  → proxy_excluded_unique_opens / proxy_excluded_open_rate
              (strips machine-generated opens, e.g. Apple Mail Privacy Protection)
            - Clicks → timing-based bot detection via email-activity; subscribers who
              clicked multiple links within 2 seconds are excluded as security-scanner
              bots. This approximates Mailchimp's "Bot filtering ON" click count.
            Set False only if you explicitly need raw unfiltered metrics.

    Returns:
        A fully formatted markdown string with all tables, subtotals, and anomaly
        flags. The agent can return this directly to the user without reformatting.
    """
    import re as _re

    # ---- Parse date -------------------------------------------------------
    date_str = target_date.strip()
    m = _re.match(r"(\d{4}-\d{2}-\d{2})", date_str)
    if not m:
        return f"ERROR: Cannot parse target_date={date_str!r}. Use YYYY-MM-DD format."
    ymd = m.group(1)
    since = f"{ymd}T00:00:00Z"
    before = f"{ymd}T23:59:59Z"

    # ---- Fetch all campaigns for the date ---------------------------------
    all_campaigns = []
    offset = 0
    page_size = 1000
    while True:
        data = _get("/campaigns", params={
            "status": "sent",
            "since_send_time": since,
            "before_send_time": before,
            "count": page_size,
            "offset": offset,
            "fields": "campaigns.id,campaigns.settings.title,campaigns.settings.subject_line,"
                      "campaigns.send_time,campaigns.emails_sent,campaigns.recipients.list_name,"
                      "campaigns.report_summary,total_items",
        })
        if not data or "campaigns" not in data:
            break
        page = data.get("campaigns", [])
        all_campaigns.extend(page)
        if len(all_campaigns) >= data.get("total_items", 0) or len(page) < page_size:
            break
        offset += page_size

    if not all_campaigns:
        return f"No sent campaigns found for {ymd}."

    # ---- Fetch individual reports -----------------------------------------
    rows = []
    for camp in all_campaigns:
        cid = camp.get("id", "")
        title = camp.get("settings", {}).get("title") or camp.get("settings", {}).get("subject_line", cid)
        subject = camp.get("settings", {}).get("subject_line", "")
        audience = camp.get("recipients", {}).get("list_name", "")
        send_time = camp.get("send_time", "")

        row = {
            "id": cid,
            "name": title,
            "subject": subject,
            "audience": audience,
            "send_time": send_time,
            "emails_sent": 0,
            "delivered": 0,
            # Opens — raw and bot-filtered
            "raw_unique_opens": 0,
            "raw_total_opens": 0,
            "raw_open_rate": 0.0,
            "bot_unique_opens": 0,
            "bot_total_opens": 0,
            "bot_open_rate": 0.0,
            # Clicks — raw and bot-filtered
            "raw_unique_clickers": 0,
            "raw_total_clicks": 0,
            "raw_click_rate": 0.0,
            "bot_unique_clickers": 0,
            "bot_click_rate": 0.0,
            "hard_bounces": 0,
            "soft_bounces": 0,
            "unsubscribes": 0,
            "abuse_reports": 0,
            "forwards_count": 0,
            "forwards_opens": 0,
            "report_error": None,
        }

        if cid:
            rdata = _get(f"/reports/{cid}")
            if rdata and "error" not in rdata:
                opens = rdata.get("opens", {})
                clicks = rdata.get("clicks", {})
                bounces = rdata.get("bounces", {})
                fwds = rdata.get("forwards", {})
                sent = rdata.get("emails_sent", 0)
                hb = bounces.get("hard_bounces", 0)
                sb = bounces.get("soft_bounces", 0)
                delivered = sent - hb - sb

                # Raw opens
                raw_u_opens = opens.get("unique_opens", 0)
                raw_t_opens = opens.get("opens_total", 0)
                raw_o_rate  = round(opens.get("open_rate", 0) * 100, 2)
                # Bot-filtered opens (proxy_excluded = removes Apple MPP / machine opens)
                bot_u_opens = opens.get("proxy_excluded_unique_opens", raw_u_opens)
                bot_t_opens = opens.get("proxy_excluded_opens", raw_t_opens)
                bot_o_rate  = round(opens.get("proxy_excluded_open_rate", opens.get("open_rate", 0)) * 100, 2)

                # Raw clicks
                raw_u_clicks = clicks.get("unique_subscriber_clicks", 0)
                raw_t_clicks = clicks.get("clicks_total", 0)
                raw_c_rate   = round((raw_u_clicks / delivered * 100) if delivered else 0, 2)
                # Bot-filtered clicks (timing analysis: multi-link clicks within 2s = bot scanner)
                bot_u_clicks = _bot_filtered_clickers(cid, raw_u_clicks)
                bot_c_rate   = round((bot_u_clicks / delivered * 100) if delivered else 0, 2)

                row.update({
                    "emails_sent": sent,
                    "delivered": delivered,
                    "raw_unique_opens": raw_u_opens,
                    "raw_total_opens": raw_t_opens,
                    "raw_open_rate": raw_o_rate,
                    "bot_unique_opens": bot_u_opens,
                    "bot_total_opens": bot_t_opens,
                    "bot_open_rate": bot_o_rate,
                    "raw_unique_clickers": raw_u_clicks,
                    "raw_total_clicks": raw_t_clicks,
                    "raw_click_rate": raw_c_rate,
                    "bot_unique_clickers": bot_u_clicks,
                    "bot_click_rate": bot_c_rate,
                    "hard_bounces": hb,
                    "soft_bounces": sb,
                    "unsubscribes": rdata.get("unsubscribed", 0),
                    "abuse_reports": rdata.get("abuse_reports", 0),
                    "forwards_count": fwds.get("forwards_count", 0),
                    "forwards_opens": fwds.get("forwards_opens", 0),
                })
            else:
                row["report_error"] = rdata.get("detail", "report unavailable") if rdata else "report unavailable"

        rows.append(row)

    # ---- Region grouping --------------------------------------------------
    def _region(name: str) -> str:
        n = (name or "").upper()
        if n.startswith("APAC"):
            return "APAC"
        if n.startswith("EU"):
            return "EU"
        if n.startswith("USMM"):
            return "USMM"
        if n.startswith("US_ENT"):
            return "US ENT"
        if n.startswith("CM_APS"):
            return "Content Marketing APS"
        return "Other"

    from collections import defaultdict as _dd
    groups = _dd(list)
    for r in rows:
        groups[_region(r["name"])].append(r)

    region_order = ["APAC", "EU", "USMM", "US ENT", "Content Marketing APS", "Other"]

    # ---- Helpers ----------------------------------------------------------
    def _pct(num, den):
        return f"{round(num / den * 100, 2):.2f}%" if den else "0.00%"

    def _fmt_row(r):
        bounce_total = r["hard_bounces"] + r["soft_bounces"]
        bounce_rate = _pct(bounce_total, r["emails_sent"])
        st = r["send_time"][:16].replace("T", " ") if r["send_time"] else ""
        err = f" ⚠️ {r['report_error']}" if r["report_error"] else ""
        return (
            f"| {r['name']}{err} | {r.get('subject', '')} | {r.get('audience', '')} | {st} | "
            f"{r['emails_sent']:,} | {r['delivered']:,} | "
            f"{r['raw_unique_opens']:,} | {r['raw_total_opens']:,} | {r['raw_open_rate']:.2f}% | "
            f"{r['bot_unique_opens']:,} | {r['bot_total_opens']:,} | {r['bot_open_rate']:.2f}% | "
            f"{r['raw_unique_clickers']:,} | {r['raw_total_clicks']:,} | {r['raw_click_rate']:.2f}% | "
            f"{r['bot_unique_clickers']:,} | {r['bot_click_rate']:.2f}% | "
            f"{r['soft_bounces']:,} | {r['hard_bounces']:,} | {bounce_rate} | "
            f"{r['unsubscribes']} | {r['abuse_reports']} | "
            f"{r['forwards_count']} / {r['forwards_opens']} |"
        )

    header = (
        "| Campaign | Subject Line | Audience | Send Time (UTC) | Sent | Delivered | "
        "Unique Opens (Raw) | Total Opens (Raw) | Open Rate (Raw) | "
        "Unique Opens (Bot Filtered) | Total Opens (Bot Filtered) | Open Rate (Bot Filtered) | "
        "Unique Clickers (Raw) | Total Clicks | Click Rate (Raw) | "
        "Unique Clickers (Bot Filtered) | Click Rate (Bot Filtered) | "
        "Soft Bounces | Hard Bounces | Bounce Rate | Unsubs | Abuse | Fwds |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    )

    def _subtotal_row(region_rows, label="**SUBTOTAL**"):
        s   = sum(r["emails_sent"] for r in region_rows)
        d   = sum(r["delivered"] for r in region_rows)
        ruo = sum(r["raw_unique_opens"] for r in region_rows)
        rto = sum(r["raw_total_opens"] for r in region_rows)
        buo = sum(r["bot_unique_opens"] for r in region_rows)
        bto = sum(r["bot_total_opens"] for r in region_rows)
        ruc = sum(r["raw_unique_clickers"] for r in region_rows)
        rtc = sum(r["raw_total_clicks"] for r in region_rows)
        buc = sum(r["bot_unique_clickers"] for r in region_rows)
        sb  = sum(r["soft_bounces"] for r in region_rows)
        hb  = sum(r["hard_bounces"] for r in region_rows)
        un  = sum(r["unsubscribes"] for r in region_rows)
        ab  = sum(r["abuse_reports"] for r in region_rows)
        fw_c = sum(r["forwards_count"] for r in region_rows)
        fw_o = sum(r["forwards_opens"] for r in region_rows)
        return (
            f"| {label} | | | — | {s:,} | {d:,} | "
            f"{ruo:,} | {rto:,} | {_pct(ruo, d)} | "
            f"{buo:,} | {bto:,} | {_pct(buo, d)} | "
            f"{ruc:,} | {rtc:,} | {_pct(ruc, d)} | "
            f"{buc:,} | {_pct(buc, d)} | "
            f"{sb:,} | {hb:,} | {_pct(sb + hb, s)} | "
            f"{un} | {ab} | {fw_c} / {fw_o} |"
        )

    # ---- Build markdown ---------------------------------------------------
    lines = [f"# Campaign Performance Report — {ymd}", ""]
    lines.append(
        "> **Raw** columns show unfiltered API data. "
        "**Bot Filtered** columns use Mailchimp proxy-excluded opens and "
        "timing-based click filtering (subscribers who clicked multiple links "
        "within 2 seconds are excluded as security-scanner bots)."
    )
    lines.append("")
    lines.append(f"**Total campaigns found:** {len(rows)}")
    lines.append("")

    grand_all = []
    for region in region_order:
        region_rows = groups.get(region)
        if not region_rows:
            continue
        lines.append(f"## {region} ({len(region_rows)} campaigns)")
        lines.append("")
        lines.append(header)
        for r in sorted(region_rows, key=lambda x: x["send_time"]):
            lines.append(_fmt_row(r))
        lines.append(_subtotal_row(region_rows))
        lines.append("")
        grand_all.extend(region_rows)

    # Grand total
    lines.append("## Grand Total")
    lines.append("")
    lines.append(header)
    lines.append(_subtotal_row(grand_all, "**GRAND TOTAL**"))
    lines.append("")

    # ---- Anomaly detection (uses bot-filtered metrics for accuracy) --------
    anomalies = []
    for r in rows:
        sent = r["emails_sent"]
        if not sent:
            continue
        bounce_total = r["hard_bounces"] + r["soft_bounces"]
        bounce_pct = bounce_total / sent * 100
        open_pct = r["bot_open_rate"]
        click_pct = r["bot_click_rate"]
        unsubs = r["unsubscribes"]
        flags = []
        if bounce_pct > bounce_rate_threshold:
            flags.append(f"Bounce rate {bounce_pct:.1f}% > {bounce_rate_threshold}% — list hygiene")
        if click_pct == 0 and open_pct > 20:
            flags.append(f"0% clicks despite {open_pct:.1f}% opens — possible tracking link error")
        if open_pct < open_rate_threshold:
            flags.append(f"Open rate {open_pct:.1f}% < {open_rate_threshold}% — review subject/send time")
        if unsubs >= unsubscribe_threshold:
            flags.append(f"{unsubs} unsubscribes ≥ threshold ({unsubscribe_threshold})")
        if flags:
            anomalies.append((r["name"], flags))

    lines.append("## ⚠️ Anomaly Flags")
    lines.append("")
    if anomalies:
        for cname, flags in anomalies:
            lines.append(f"**{cname}**")
            for f in flags:
                lines.append(f"  - {f}")
            lines.append("")
    else:
        lines.append("_No anomalies detected._")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Account Exports
# ---------------------------------------------------------------------------

@mcp.tool()
def mailchimp_create_account_export(
    include_stages: Optional[List[str]] = None,
    since_timestamp: Optional[str] = None,
) -> str:
    """
    Create a new Mailchimp account export job. The export compiles account
    data (subscribers, campaign activity, etc.) into a downloadable archive.
    After creating, use mailchimp_get_account_export to poll until it completes,
    then download the file from the returned URL.

    Args:
        include_stages: Optional list of export stages to include.
                        e.g. ["subscribe", "unsubscribe", "cleaned",
                              "email_address", "campaign_abuse",
                              "campaign_unsubscribe", "campaign_activity"]
                        Leave empty to export everything.
        since_timestamp: ISO 8601 date string to restrict export to records
                         changed since this date. e.g. "2026-01-01T00:00:00+00:00"

    Returns:
        JSON string with the new export job details including export_id and status.
    """
    body: dict = {}
    if include_stages:
        body["include_stages"] = include_stages
    if since_timestamp:
        body["since_timestamp"] = since_timestamp
    result = _post("/account-exports", body)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_list_account_exports(
    count: Optional[int] = None,
    offset: Optional[int] = None,
) -> str:
    """
    List all account export jobs for the connected Mailchimp account,
    most recent first. Use this to find existing exports or check their status.

    Args:
        count:  Number of exports to return (max 1000). Default 10.
        offset: Number of exports to skip (for pagination). Default 0.

    Returns:
        JSON string with a list of export jobs, each including export_id,
        status ("pending" | "building" | "finished" | "failed"), creation
        timestamp, and download URL (populated when status = "finished").
    """
    params: dict = {}
    if count is not None:
        params["count"] = count
    if offset is not None:
        params["offset"] = offset
    result = _get("/account-exports", params=params if params else None)
    return json.dumps(result, indent=2)


@mcp.tool()
def mailchimp_get_account_export(export_id: str) -> str:
    """
    Get the status and details of a specific Mailchimp account export job.
    Poll this after calling mailchimp_create_account_export until
    status = "finished", then use the returned download URL to retrieve the file.

    Args:
        export_id: The unique ID of the export job (returned by
                   mailchimp_create_account_export or mailchimp_list_account_exports).

    Returns:
        JSON string with export details including:
          - export_id: unique identifier
          - status: "pending" | "building" | "finished" | "failed"
          - created_at / finished_at: timestamps
          - download_url: present and usable when status = "finished"
    """
    result = _get(f"/account-exports/{export_id}")
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
