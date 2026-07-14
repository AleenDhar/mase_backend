import os
import json
import time
import threading
from typing import Optional, List, Any

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("avoma-mcp-server")

TOKEN = os.environ.get("AVOMA_API_TOKEN", "ifi116h6e8:2p7r6khoxqojr5638sld")
BASE_URL = "https://api.avoma.com/v1"

_api_lock = threading.Lock()
_last_request_time = 0.0
_MIN_REQUEST_INTERVAL = 0.15
_MAX_429_RETRIES = int(os.environ.get("AVOMA_MAX_429_RETRIES", "5"))
# Per-request HTTP timeout (seconds). In isolation Avoma calls return in ~4s,
# but under concurrent load (the live webhook analyzer + a sweep both hitting
# Avoma) responses slow down and the old 30s cap caused "read operation timed
# out" failures. Raised + made configurable so legitimate slow-under-load calls
# survive. Override with AVOMA_HTTP_TIMEOUT.
_HTTP_TIMEOUT = float(os.environ.get("AVOMA_HTTP_TIMEOUT", "60"))
# Retries on read/connect timeouts and transient transport errors (separate
# budget from the 429 retries). Override with AVOMA_MAX_TIMEOUT_RETRIES.
_MAX_TIMEOUT_RETRIES = int(os.environ.get("AVOMA_MAX_TIMEOUT_RETRIES", "2"))


def _throttle():
    global _last_request_time
    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
    _last_request_time = time.monotonic()


def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def _send_with_retry(method: str, url: str, *, params=None, json_body=None,
                     headers=None) -> httpx.Response:
    """Issue an Avoma request, retrying on HTTP 429 with Retry-After/backoff.

    Honours the `Retry-After` header when present; otherwise exponential
    backoff (1, 2, 4, 8...s capped at 30s). Each attempt is throttled +
    serialised via `_api_lock` so concurrent callers (e.g. the opportunity
    analyzer firing many transcript/notes GETs) don't hammer Avoma. The
    backoff sleep happens outside the lock. Retries up to `_MAX_429_RETRIES`.
    """
    resp = None
    rate_retries = 0
    timeout_retries = 0
    max_attempts = _MAX_429_RETRIES + _MAX_TIMEOUT_RETRIES + 1
    for _ in range(max_attempts):
        try:
            with _api_lock:
                _throttle()
                with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
                    resp = client.request(method, url, headers=headers,
                                          params=params, json=json_body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # Read/connect timeout or transient transport error. Retry with
            # exponential backoff (sleep outside the lock) up to the budget,
            # then re-raise so the caller surfaces a real failure.
            if timeout_retries >= _MAX_TIMEOUT_RETRIES:
                raise
            delay = max(0.5, min(2 ** timeout_retries, 30.0))
            timeout_retries += 1
            print(f"[AVOMA-TIMEOUT] {method} {url} attempt {timeout_retries}/"
                  f"{_MAX_TIMEOUT_RETRIES} ({type(exc).__name__}) — "
                  f"sleeping {delay:.1f}s", flush=True)
            time.sleep(delay)
            continue
        if resp.status_code != 429 or rate_retries >= _MAX_429_RETRIES:
            return resp
        ra = resp.headers.get("Retry-After")
        try:
            delay = float(ra) if ra else float(min(2 ** rate_retries, 30))
        except (TypeError, ValueError):
            delay = float(min(2 ** rate_retries, 30))
        delay = max(0.5, min(delay, 30.0))
        rate_retries += 1
        print(f"[AVOMA-429] {method} {url} attempt {rate_retries}/"
              f"{_MAX_429_RETRIES} — sleeping {delay:.1f}s", flush=True)
        time.sleep(delay)
    return resp


def _get(endpoint: str, params: Optional[dict] = None) -> Any:
    url = f"{BASE_URL}{endpoint}"
    resp = _send_with_retry("GET", url, params=params, headers=_headers())
    if resp.status_code == 404:
        return {
            "error": f"Not found: {endpoint}. The resource may not exist or hasn't been processed yet."
        }
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    try:
        return resp.json()
    except Exception:
        return {"error": "Failed to parse API response", "raw": resp.text}


def _patch(endpoint: str, body: dict) -> Any:
    url = f"{BASE_URL}{endpoint}"
    headers = {**_headers(), "Content-Type": "application/json"}
    resp = _send_with_retry("PATCH", url, json_body=body, headers=headers)
    try:
        parsed = resp.json()
    except Exception:
        parsed = {"raw": resp.text}

    # Derive a best-effort human-readable error message from Avoma's body.
    # Avoma is inconsistent: sometimes `{"message": "..."}`, sometimes
    # field-keyed like `{"purpose": "... field update is not allowed ..."}`,
    # sometimes plain text. Try all three.
    def _extract_message(p, fallback_text: str) -> Optional[str]:
        if isinstance(p, dict):
            if isinstance(p.get("message"), str):
                return p["message"]
            for k, v in p.items():
                if isinstance(v, str) and v:
                    return f"{k}: {v}"
            if p.get("raw"):
                return str(p["raw"])
        return fallback_text or None

    avoma_message = _extract_message(parsed, resp.text)

    if resp.status_code == 406:
        # Only label as the "after meeting starts" block when the body
        # actually says so. Otherwise return a generic 406 with full body.
        msg_lc = (avoma_message or "").lower()
        if "after meeting starts" in msg_lc or "meeting has started" in msg_lc:
            return {
                "error": "avoma_write_blocked_after_meeting_started",
                "status": 406,
                "avoma_message": avoma_message,
                "body": parsed,
                "explanation": (
                    "Avoma rejects PATCH writes to this field on meetings "
                    "that have already started. Only the Avoma web UI can "
                    "change it for past meetings. This tool succeeds for "
                    "meetings whose start_at is still in the future."
                ),
                "endpoint": endpoint,
                "body_sent": body,
            }
        return {
            "error": "API error 406",
            "status": 406,
            "avoma_message": avoma_message,
            "body": parsed,
            "endpoint": endpoint,
            "body_sent": body,
        }
    if resp.status_code == 404:
        return {
            "error": f"Not found: {endpoint}",
            "status": 404,
            "avoma_message": avoma_message,
            "body": parsed,
        }
    if resp.status_code >= 400:
        return {
            "error": f"API error {resp.status_code}",
            "status": resp.status_code,
            "avoma_message": avoma_message,
            "body": parsed,
        }
    return parsed


def _get_all_pages(endpoint: str, params: dict, max_pages: int = 50) -> List[dict]:
    """
    Automatically paginate through all pages of a list endpoint and return
    all results combined. Stops at max_pages to prevent runaway loops.
    """
    all_results = []
    page = 1
    while page <= max_pages:
        page_params = {**params, "page": page, "page_size": 100}
        data = _get(endpoint, page_params)
        if "error" in data:
            break
        results = data.get("results", [])
        all_results.extend(results)
        count = data.get("count", 0)
        if len(all_results) >= count or not data.get("next"):
            break
        page += 1
    return all_results


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------

@mcp.tool()
def list_meetings(
    from_date: str,
    to_date: str,
    crm_account_ids: Optional[str] = None,
    crm_opportunity_ids: Optional[str] = None,
    crm_contact_ids: Optional[str] = None,
    crm_lead_ids: Optional[str] = None,
    attendee_emails: Optional[str] = None,
    is_internal: Optional[bool] = None,
    is_call: Optional[bool] = None,
    include_crm_associations: bool = False,
    page: int = 1,
    page_size: int = 100,
    order: str = "-start_at",
) -> dict:
    """
    List meetings from Avoma with optional filters. Returns one page of results.
    For accounts/opportunities with many calls, use get_all_meetings_for_account
    or get_all_meetings_for_opportunity instead — those auto-paginate to return
    ALL calls without missing any.

    Args:
        from_date: Start date-time in ISO format (e.g. "2025-01-01T00:00:00Z")
        to_date: End date-time in ISO format (e.g. "2025-12-31T23:59:59Z")
        crm_account_ids: Comma-separated Salesforce Account IDs (e.g. "001ABC,001DEF")
        crm_opportunity_ids: Comma-separated Salesforce Opportunity IDs (e.g. "006ABC")
        crm_contact_ids: Comma-separated CRM contact external IDs
        crm_lead_ids: Comma-separated CRM lead external IDs
        attendee_emails: Comma-separated attendee emails to filter by
        is_internal: True for internal-only meetings, False for external meetings
        is_call: True for voice calls only, False for video calls only
        include_crm_associations: Include CRM associations in response (default False)
        page: Page number to retrieve (default 1)
        page_size: Results per page (default 100, max 100)
        order: Sort order — "start_at" ascending or "-start_at" descending (default)
    """
    params: dict = {
        "from_date": from_date,
        "to_date": to_date,
        "page": page,
        "page_size": min(page_size, 100),
        "o": order,
    }
    if crm_account_ids:
        params["crm_account_ids"] = crm_account_ids
    if crm_opportunity_ids:
        params["crm_opportunity_ids"] = crm_opportunity_ids
    if crm_contact_ids:
        params["crm_contact_ids"] = crm_contact_ids
    if crm_lead_ids:
        params["crm_lead_ids"] = crm_lead_ids
    if attendee_emails:
        params["attendee_emails"] = attendee_emails
    if is_internal is not None:
        params["is_internal"] = is_internal
    if is_call is not None:
        params["is_call"] = is_call
    if include_crm_associations:
        params["include_crm_associations"] = True
    return _get("/meetings/", params)


@mcp.tool()
def get_all_meetings_for_account(
    crm_account_id: str,
    from_date: str,
    to_date: str,
    is_call: Optional[bool] = None,
    include_crm_associations: bool = True,
    order: str = "-start_at",
) -> dict:
    """
    Retrieve ALL meetings/calls associated with a specific CRM account, automatically
    paginating through every page so no calls are missed.

    Use this instead of list_meetings when you need a complete history of calls for
    an account. It handles pagination automatically and returns every meeting Avoma
    has on record for that account within the date range.

    Args:
        crm_account_id: The Salesforce (or CRM) Account ID (e.g. "0012300000AbcDef").
        from_date: Start date-time in ISO format (e.g. "2024-01-01T00:00:00Z").
        to_date: End date-time in ISO format (e.g. "2025-12-31T23:59:59Z").
        is_call: True for voice calls only, False for video calls only, None for all.
        include_crm_associations: Include CRM associations in response (default True).
        order: Sort order — "start_at" ascending or "-start_at" descending (default).

    Returns:
        Dict with "total_count" and "meetings" list containing all meetings found.
    """
    params: dict = {
        "from_date": from_date,
        "to_date": to_date,
        "crm_account_ids": crm_account_id,
        "o": order,
    }
    if is_call is not None:
        params["is_call"] = is_call
    if include_crm_associations:
        params["include_crm_associations"] = True

    all_meetings = _get_all_pages("/meetings/", params)
    return {
        "total_count": len(all_meetings),
        "crm_account_id": crm_account_id,
        "from_date": from_date,
        "to_date": to_date,
        "meetings": all_meetings,
    }


@mcp.tool()
def get_all_meetings_for_opportunity(
    crm_opportunity_id: str,
    from_date: str,
    to_date: str,
    is_call: Optional[bool] = None,
    include_crm_associations: bool = True,
    order: str = "-start_at",
) -> dict:
    """
    Retrieve ALL meetings/calls associated with a specific CRM opportunity, automatically
    paginating through every page so no calls are missed.

    Use this instead of list_meetings when you need a complete call history for an
    opportunity. It handles pagination automatically and returns every meeting Avoma
    has on record for that opportunity within the date range.

    Args:
        crm_opportunity_id: The Salesforce (or CRM) Opportunity ID (e.g. "0062300000XyzAbc").
        from_date: Start date-time in ISO format (e.g. "2024-01-01T00:00:00Z").
        to_date: End date-time in ISO format (e.g. "2025-12-31T23:59:59Z").
        is_call: True for voice calls only, False for video calls only, None for all.
        include_crm_associations: Include CRM associations in response (default True).
        order: Sort order — "start_at" ascending or "-start_at" descending (default).

    Returns:
        Dict with "total_count" and "meetings" list containing all meetings found.
    """
    params: dict = {
        "from_date": from_date,
        "to_date": to_date,
        "crm_opportunity_ids": crm_opportunity_id,
        "o": order,
    }
    if is_call is not None:
        params["is_call"] = is_call
    if include_crm_associations:
        params["include_crm_associations"] = True

    all_meetings = _get_all_pages("/meetings/", params)
    return {
        "total_count": len(all_meetings),
        "crm_opportunity_id": crm_opportunity_id,
        "from_date": from_date,
        "to_date": to_date,
        "meetings": all_meetings,
    }


@mcp.tool()
def get_all_meetings_for_attendee(
    email: str,
    from_date: str,
    to_date: str,
    is_internal: Optional[bool] = None,
    is_call: Optional[bool] = None,
    include_crm_associations: bool = True,
    order: str = "-start_at",
) -> dict:
    """
    Retrieve ALL meetings/calls attended by a specific person (by email), automatically
    paginating through every page so no calls are missed.

    Args:
        email: The attendee's email address (e.g. "john.doe@company.com").
        from_date: Start date-time in ISO format (e.g. "2024-01-01T00:00:00Z").
        to_date: End date-time in ISO format (e.g. "2025-12-31T23:59:59Z").
        is_internal: True for internal meetings only, False for external only. None for all.
        is_call: True for voice calls only, False for video only. None for all.
        include_crm_associations: Include CRM associations in response (default True).
        order: Sort order — "start_at" ascending or "-start_at" descending (default).

    Returns:
        Dict with "total_count" and "meetings" list containing all meetings found.
    """
    params: dict = {
        "from_date": from_date,
        "to_date": to_date,
        "attendee_emails": email,
        "o": order,
    }
    if is_internal is not None:
        params["is_internal"] = is_internal
    if is_call is not None:
        params["is_call"] = is_call
    if include_crm_associations:
        params["include_crm_associations"] = True

    all_meetings = _get_all_pages("/meetings/", params)
    return {
        "total_count": len(all_meetings),
        "email": email,
        "from_date": from_date,
        "to_date": to_date,
        "meetings": all_meetings,
    }


@mcp.tool()
def get_meeting(uuid: str, include_crm_associations: bool = True) -> dict:
    """
    Get detailed information about a specific meeting.

    Args:
        uuid: Meeting UUID identifier.
        include_crm_associations: Include CRM associations in the response (default True).
    """
    params = {}
    if include_crm_associations:
        params["include_crm_associations"] = True
    return _get(f"/meetings/{uuid}/", params if params else None)


@mcp.tool()
def get_meeting_transcript(uuid: str) -> dict:
    """
    Get the full transcript of a meeting including speaker turns and timestamps.

    Args:
        uuid: Meeting UUID identifier.
    """
    meeting = _get(f"/meetings/{uuid}/")
    if "error" in meeting:
        return meeting
    transcription_uuid = meeting.get("transcription_uuid")
    if not transcription_uuid:
        return {
            "error": "No transcript available for this meeting. "
                     "The call may not have been recorded or has not finished processing.",
            "transcript_ready": meeting.get("transcript_ready"),
            "processing_status": meeting.get("processing_status"),
        }
    return _get(f"/transcriptions/{transcription_uuid}/")


@mcp.tool()
def get_meeting_notes(uuid: str) -> dict:
    """
    Get structured AI-generated notes for a meeting. Returns all note types
    (action items, key takeaways, questions, next steps, follow-ups, etc.)
    extracted by Avoma's AI, along with speaker information.

    Args:
        uuid: Meeting UUID identifier.
    """
    data = _get(f"/meetings/{uuid}/insights/")
    if "error" in data:
        return data
    ai_notes = data.get("ai_notes", [])
    if not ai_notes:
        return {
            "uuid": uuid,
            "notes": [],
            "message": "No AI-generated notes available for this meeting.",
        }
    return {
        "uuid": uuid,
        "total_notes": len(ai_notes),
        "notes": ai_notes,
        "speakers": data.get("speakers", []),
    }


@mcp.tool()
def get_meeting_insights(meeting_uuid: str) -> dict:
    """
    Get AI-generated insights for a meeting including keywords, talk ratios,
    speaker breakdown, and AI-generated notes. Meeting must be in completed state.

    Args:
        meeting_uuid: Meeting UUID identifier.
    """
    return _get(f"/meetings/{meeting_uuid}/insights/")


@mcp.tool()
def get_meeting_segments(uuid: str) -> dict:
    """
    Get topic segments of a meeting (e.g. intro, demo, pricing, next_steps).

    Args:
        uuid: Meeting UUID identifier.
    """
    return _get("/meeting_segments/", {"uuid": uuid})


@mcp.tool()
def get_meeting_recording_url(uuid: str) -> dict:
    """
    Get the recording URL for a specific meeting.

    Args:
        uuid: Meeting UUID identifier.

    Returns:
        Dict containing the recording URL and related metadata.
    """
    meeting = _get(f"/meetings/{uuid}/")
    if "error" in meeting:
        return meeting
    recording_uuid = meeting.get("recording_uuid")
    if not recording_uuid:
        return {
            "error": "No recording available for this meeting. "
                     "The call may not have been recorded or has not finished processing.",
            "audio_ready": meeting.get("audio_ready"),
            "video_ready": meeting.get("video_ready"),
            "processing_status": meeting.get("processing_status"),
        }
    return _get(f"/recordings/{recording_uuid}/")


@mcp.tool()
def get_meeting_action_items(uuid: str) -> dict:
    """
    Get action items extracted from a meeting by Avoma's AI.

    Args:
        uuid: Meeting UUID identifier.

    Returns:
        Dict with list of action items (owner, text, timestamps) extracted by Avoma's AI.
    """
    data = _get(f"/meetings/{uuid}/insights/")
    if "error" in data:
        return data
    ai_notes = data.get("ai_notes", [])
    speakers = data.get("speakers", [])
    speaker_map = {s["id"]: s for s in speakers}
    action_items = [n for n in ai_notes if n.get("note_type") == "action_item"]
    for item in action_items:
        sid = item.get("speaker_id")
        if sid is not None and sid in speaker_map:
            item["speaker_name"] = speaker_map[sid].get("name")
            item["speaker_email"] = speaker_map[sid].get("email")
    return {
        "uuid": uuid,
        "total_action_items": len(action_items),
        "action_items": action_items,
        "speakers": speakers,
    }


@mcp.tool()
def get_meetings_summary_for_account(
    crm_account_id: str,
    from_date: str,
    to_date: str,
) -> dict:
    """
    Get a concise summary of all calls for a CRM account — total count, date range
    of calls, and key metadata per call (title, date, duration, attendees).
    Useful for a quick overview before diving into individual call details.

    Args:
        crm_account_id: The Salesforce (or CRM) Account ID.
        from_date: Start date-time in ISO format (e.g. "2024-01-01T00:00:00Z").
        to_date: End date-time in ISO format (e.g. "2025-12-31T23:59:59Z").

    Returns:
        Dict with total_count and a summary list of meetings with key fields only.
    """
    params: dict = {
        "from_date": from_date,
        "to_date": to_date,
        "crm_account_ids": crm_account_id,
        "include_crm_associations": True,
        "o": "-start_at",
    }
    all_meetings = _get_all_pages("/meetings/", params)

    # Pre-compute the IST clock time so the chat model never does UTC->IST math itself
    # (RCA 2026-07-15: model-side +5:30 arithmetic was the source of wrong-time answers).
    from datetime import datetime as _dt, timedelta as _td

    def _ist(u):
        if not u:
            return None
        try:
            return (_dt.fromisoformat(str(u).replace("Z", "+00:00")) + _td(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M IST")
        except Exception:  # noqa: BLE001
            return None

    summary = []
    for m in all_meetings:
        _tu = m.get("transcription_uuid")
        summary.append({
            "uuid": m.get("uuid"),
            "title": m.get("title"),
            "start_at": m.get("start_at"),                # UTC, exactly as Avoma returns it
            "start_at_ist": _ist(m.get("start_at")),      # pre-computed IST — cite this, don't recompute
            "duration_seconds": m.get("duration"),
            "is_call": m.get("is_call"),
            "state": m.get("state"),
            # Transcript availability as a FACT (not a title guess): exists iff transcript_ready
            # is true OR a transcription_uuid is present.
            "transcript_ready": bool(m.get("transcript_ready")) or bool(_tu),
            "transcription_uuid": _tu,
            "recording_uuid": m.get("recording_uuid"),
            "attendees": [
                a.get("email") for a in m.get("attendees", [])
            ],
            "crm_associations": m.get("crm_associations", {}),
        })

    return {
        "crm_account_id": crm_account_id,
        "total_count": len(summary),
        "from_date": from_date,
        "to_date": to_date,
        "meetings": summary,
    }


@mcp.tool()
def get_meetings_summary_for_opportunity(
    crm_opportunity_id: str,
    from_date: str,
    to_date: str,
) -> dict:
    """
    Get a concise summary of all calls for a CRM opportunity — total count and
    key metadata per call (title, date, duration, attendees).
    Useful for a quick overview of the opportunity's call history.

    Args:
        crm_opportunity_id: The Salesforce (or CRM) Opportunity ID.
        from_date: Start date-time in ISO format (e.g. "2024-01-01T00:00:00Z").
        to_date: End date-time in ISO format (e.g. "2025-12-31T23:59:59Z").

    Returns:
        Dict with total_count and a summary list of meetings with key fields only.
    """
    params: dict = {
        "from_date": from_date,
        "to_date": to_date,
        "crm_opportunity_ids": crm_opportunity_id,
        "include_crm_associations": True,
        "o": "-start_at",
    }
    all_meetings = _get_all_pages("/meetings/", params)

    # Pre-compute the IST clock time so the chat model never does UTC->IST math itself
    # (RCA 2026-07-15: model-side +5:30 arithmetic was the source of wrong-time answers).
    from datetime import datetime as _dt, timedelta as _td

    def _ist(u):
        if not u:
            return None
        try:
            return (_dt.fromisoformat(str(u).replace("Z", "+00:00")) + _td(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M IST")
        except Exception:  # noqa: BLE001
            return None

    summary = []
    for m in all_meetings:
        _tu = m.get("transcription_uuid")
        summary.append({
            "uuid": m.get("uuid"),
            "title": m.get("title"),
            "start_at": m.get("start_at"),                # UTC, exactly as Avoma returns it
            "start_at_ist": _ist(m.get("start_at")),      # pre-computed IST — cite this, don't recompute
            "duration_seconds": m.get("duration"),
            "is_call": m.get("is_call"),
            "state": m.get("state"),
            # Transcript availability as a FACT (not a title guess): exists iff transcript_ready
            # is true OR a transcription_uuid is present.
            "transcript_ready": bool(m.get("transcript_ready")) or bool(_tu),
            "transcription_uuid": _tu,
            "recording_uuid": m.get("recording_uuid"),
            "attendees": [
                a.get("email") for a in m.get("attendees", [])
            ],
            "crm_associations": m.get("crm_associations", {}),
        })

    return {
        "crm_opportunity_id": crm_opportunity_id,
        "total_count": len(summary),
        "from_date": from_date,
        "to_date": to_date,
        "meetings": summary,
    }


# ---------------------------------------------------------------------------
# Write tools (PATCH) — meeting purpose tagging
# ---------------------------------------------------------------------------
#
# Avoma's API supports PATCH /v1/meetings/{uuid}/ but blocks `purpose` writes
# on meetings that have already started (HTTP 406). These tools work for
# meetings whose start_at is still in the future. For past meetings the
# Avoma web UI is the only way.

@mcp.tool()
def avoma_list_known_purposes(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    max_pages: int = 5,
) -> dict:
    """
    Discover purpose UUIDs + labels in use in this Avoma workspace by
    scanning recent meetings. Avoma has NO public /purposes/ endpoint, so
    discovery is by harvest: only purposes that have actually been used on
    at least one meeting in the scanned window will appear.

    Args:
        from_date: ISO date (YYYY-MM-DD). Defaults to 180 days ago.
        to_date: ISO date (YYYY-MM-DD). Defaults to today.
        max_pages: cap on pages of 100 to scan. Default 5 (= 500 meetings).
    """
    from datetime import datetime, timedelta, timezone
    if not to_date:
        to_date = datetime.now(timezone.utc).date().isoformat()
    if not from_date:
        from_date = (
            datetime.now(timezone.utc).date() - timedelta(days=180)
        ).isoformat()

    meetings = _get_all_pages(
        "/meetings/",
        {"from_date": from_date, "to_date": to_date},
        max_pages=max_pages,
    )
    purposes: dict = {}
    for m in meetings:
        p = m.get("purpose")
        if isinstance(p, dict) and p.get("uuid"):
            purposes[p["uuid"]] = p.get("label")
    return {
        "from_date": from_date,
        "to_date": to_date,
        "meetings_scanned": len(meetings),
        "purpose_count": len(purposes),
        "purposes": [
            {"uuid": uid, "label": label}
            for uid, label in sorted(purposes.items(), key=lambda x: (x[1] or ""))
        ],
        "note": (
            "Avoma has no /purposes/ endpoint. This list only contains "
            "purposes that have already been assigned to at least one "
            "meeting in the scanned window. Increase max_pages or widen "
            "from_date to discover more."
        ),
    }


@mcp.tool()
def avoma_set_meeting_purpose(
    meeting_uuid: str,
    purpose_uuid: str,
) -> dict:
    """
    Set the Purpose on an Avoma meeting via PATCH /v1/meetings/{uuid}/.

    IMPORTANT — server-side restriction: Avoma rejects this write on any
    meeting whose start_at is in the past with HTTP 406:
        "{'purpose': '...'} field update is not allowed after meeting starts."
    For past meetings the Avoma web UI is the only way; this tool will
    surface the 406 verbatim instead of silently failing.

    Use `avoma_list_known_purposes` first to find a valid purpose_uuid.

    Args:
        meeting_uuid: the Avoma meeting UUID.
        purpose_uuid: the UUID of the Purpose to assign.

    Returns: the updated meeting record on success, or an error dict with
    `error: avoma_write_blocked_after_meeting_started` on the 406 case.
    """
    if not meeting_uuid or not purpose_uuid:
        return {"error": "Both meeting_uuid and purpose_uuid are required."}

    # Pre-flight: read the meeting so we can warn the agent before the API
    # round-trip if start_at is already in the past.
    existing = _get(f"/meetings/{meeting_uuid}/")
    if isinstance(existing, dict) and existing.get("error"):
        return {
            "error": "meeting_not_found_or_unreadable",
            "meeting_uuid": meeting_uuid,
            "detail": existing.get("error"),
        }

    from datetime import datetime, timezone
    start_at = existing.get("start_at") if isinstance(existing, dict) else None
    started_already = False
    if start_at:
        try:
            start_dt = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
            started_already = start_dt <= datetime.now(timezone.utc)
        except Exception:
            started_already = False

    result = _patch(
        f"/meetings/{meeting_uuid}/",
        {"purpose": {"uuid": purpose_uuid}},
    )

    if isinstance(result, dict) and result.get("error") == \
            "avoma_write_blocked_after_meeting_started":
        result["meeting_uuid"] = meeting_uuid
        result["meeting_subject"] = existing.get("subject") if isinstance(existing, dict) else None
        result["meeting_start_at"] = start_at
        return result

    if isinstance(result, dict) and result.get("error"):
        return {
            **result,
            "meeting_uuid": meeting_uuid,
            "preflight_started_already": started_already,
        }

    return {
        "ok": True,
        "meeting_uuid": meeting_uuid,
        "subject": result.get("subject") if isinstance(result, dict) else None,
        "purpose": result.get("purpose") if isinstance(result, dict) else None,
        "preflight_started_already": started_already,
    }


if __name__ == "__main__":
    mcp.run()
