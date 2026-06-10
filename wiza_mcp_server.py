import os
import json
from typing import Optional, List, Any

import httpx
from fastmcp import FastMCP

BASE_URL = "https://wiza.co"
API_KEY = os.environ.get("WIZA_API_KEY", "")

mcp = FastMCP(
    name="Wiza",
    instructions=(
        "Use this server to interact with the Wiza B2B contact enrichment API. "
        "You can create contact lists, check their status, retrieve enriched contacts, "
        "build prospect lists via filters, and reveal individual contact details in real time. "
        "All operations require a valid Wiza API key set via the WIZA_API_KEY environment variable."
    ),
)


def _headers() -> dict:
    if not API_KEY:
        raise RuntimeError(
            "WIZA_API_KEY environment variable is not set. "
            "Please set it to your Wiza bearer token."
        )
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _get(path: str, params: Optional[dict] = None) -> Any:
    url = f"{BASE_URL}{path}"
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers=_headers(), params=params)
    resp.raise_for_status()
    return resp.json()


def _post(path: str, payload: dict) -> Any:
    url = f"{BASE_URL}{path}"
    with httpx.Client(timeout=30) as client:
        resp = client.post(url, headers=_headers(), json=payload)
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def wiza_create_list(
    name: str,
    items: List[dict],
    enrichment_level: str = "partial",
    accept_work_email: bool = True,
    accept_personal_email: bool = True,
    accept_generic_email: bool = False,
) -> str:
    """
    Create a contact enrichment list (bulk processing, up to 2500 contacts).

    Each item in `items` should be a dict with ONE of the following combinations:
      - {"full_name": "...", "company": "..."} or {"full_name": "...", "domain": "..."}
      - {"first_name": "...", "last_name": "...", "company": "..."} or domain
      - {"profile_url": "https://linkedin.com/in/..."} (LinkedIn / Sales Nav / Recruiter)

    Args:
        name: Descriptive name for this list, e.g. "VP of Sales in San Francisco".
        items: List of contact dicts (max 2500). See above for required fields.
        enrichment_level: "partial" (email only) or "full" (email + phone + more).
        accept_work_email: Include verified work/professional emails. Default True.
        accept_personal_email: Include personal emails. Default True.
        accept_generic_email: Include generic emails (info@, hello@, etc.). Default False.

    Returns:
        JSON string with the created list details including its ID.
    """
    payload = {
        "list": {
            "name": name,
            "enrichment_level": enrichment_level,
            "email_options": {
                "accept_work": accept_work_email,
                "accept_personal": accept_personal_email,
                "accept_generic": accept_generic_email,
            },
            "items": items,
        }
    }
    result = _post("/api/lists", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def wiza_get_list(list_id: int) -> str:
    """
    Get the status and details of a contact enrichment list by its ID.

    The list goes through several statuses: pending -> processing -> complete.
    Poll this endpoint until status is "complete" before fetching contacts.

    Args:
        list_id: The numeric ID returned when creating the list.

    Returns:
        JSON string with list metadata including status, enrichment_level, and counts.
    """
    result = _get(f"/api/lists/{list_id}")
    return json.dumps(result, indent=2)


@mcp.tool()
def wiza_get_list_contacts(
    list_id: int,
    segment: str = "people",
    page: Optional[int] = None,
) -> str:
    """
    Retrieve the enriched contacts for a completed list.

    Args:
        list_id: The numeric ID of the list.
        segment: Which segment of contacts to return (REQUIRED by the Wiza API):
            "people" - all enriched contacts (default)
            "valid"  - only valid contacts
            "risky"  - only risky contacts (e.g. risky emails)
        page: Page number for pagination (starts at 1). Omit for the first page.

    Returns:
        JSON string with an array of enriched contact objects.
        Each contact includes: email, full_name, first_name, last_name, title,
        linkedin, phone numbers, company info, and more depending on enrichment_level.
    """
    # Per Wiza OpenAPI spec (https://docs.wiza.co/swagger/v1/openapi.yaml):
    # GET /api/lists/{id}/contacts takes a REQUIRED query param `segment`
    # with enum ['people','valid','risky']. The previous code sent `filter=`
    # which Wiza rejected with HTTP 400 (chat b720c200, seqs 153-158).
    allowed = {"people", "valid", "risky"}
    if segment not in allowed:
        return json.dumps({
            "error": f"Invalid segment '{segment}'. Must be one of: {sorted(allowed)}"
        })
    params: dict = {"segment": segment}
    if page is not None:
        params["page"] = page
    result = _get(f"/api/lists/{list_id}/contacts", params=params)
    return json.dumps(result, indent=2)


@mcp.tool()
def wiza_create_prospect_list(
    name: str,
    job_titles: Optional[List[str]] = None,
    locations: Optional[List[str]] = None,
    company_sizes: Optional[List[str]] = None,
    industries: Optional[List[str]] = None,
    keywords: Optional[List[str]] = None,
    enrichment_level: str = "partial",
    accept_work_email: bool = True,
    accept_personal_email: bool = True,
    accept_generic_email: bool = False,
    limit: Optional[int] = None,
) -> str:
    """
    Create a prospect list using search filters (no need to supply individual profiles).
    Wiza searches its database and returns matching contacts.

    This is useful for building targeted lists like "VPs of Engineering in New York at
    companies with 50-200 employees in the SaaS industry".

    Args:
        name: Descriptive name for the prospect list.
        job_titles: Filter by job titles, e.g. ["VP of Sales", "Director of Sales"].
        locations: Filter by location, e.g. ["San Francisco, CA", "New York, NY"].
        company_sizes: Filter by company headcount range,
            e.g. ["1-10", "11-50", "51-200", "201-500", "501-1000", "1001-5000", "5001+"].
        industries: Filter by industry, e.g. ["Software", "Financial Services"].
        keywords: Additional keyword filters for the search.
        enrichment_level: "partial" (email only) or "full" (email + phone + more).
        accept_work_email: Include verified work/professional emails. Default True.
        accept_personal_email: Include personal emails. Default True.
        accept_generic_email: Include generic emails. Default False.
        limit: Maximum number of contacts to return.

    Returns:
        JSON string with the created prospect list details including its ID.
    """
    filters: dict = {}
    if job_titles:
        filters["job_titles"] = job_titles
    if locations:
        filters["locations"] = locations
    if company_sizes:
        filters["company_sizes"] = company_sizes
    if industries:
        filters["industries"] = industries
    if keywords:
        filters["keywords"] = keywords
    if limit:
        filters["limit"] = limit

    payload = {
        "list": {
            "name": name,
            "enrichment_level": enrichment_level,
            "email_options": {
                "accept_work": accept_work_email,
                "accept_personal": accept_personal_email,
                "accept_generic": accept_generic_email,
            },
            "filters": filters,
        }
    }
    result = _post("/api/lists/prospect", payload)
    return json.dumps(result, indent=2)


@mcp.tool()
def wiza_reveal_contact(
    linkedin_url: Optional[str] = None,
    full_name: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    company: Optional[str] = None,
    domain: Optional[str] = None,
    enrichment_level: str = "partial",
    accept_work_email: bool = True,
    accept_personal_email: bool = True,
    accept_generic_email: bool = False,
) -> str:
    """
    Reveal contact details for a SINGLE person in real time (Individual Reveal endpoint).
    This is faster than list processing but limited to 15 requests/second.

    Provide ONE of the following input combinations:
      Option A: linkedin_url (LinkedIn, Sales Nav, or Recruiter profile URL)
      Option B: (full_name OR first_name+last_name) AND (company OR domain)

    Args:
        linkedin_url: LinkedIn profile URL (preferred if available).
        full_name: Full name, e.g. "Jane Smith".
        first_name: First name (use with last_name).
        last_name: Last name (use with first_name).
        company: Company name, e.g. "Acme Corp".
        domain: Company domain, e.g. "acme.com".
        enrichment_level: "partial" (email) or "full" (email + phone + full profile).
        accept_work_email: Include professional emails. Default True.
        accept_personal_email: Include personal emails. Default True.
        accept_generic_email: Include generic emails. Default False.

    Returns:
        JSON string with enriched contact data (email, phone, title, company info, etc.)
    """
    if not linkedin_url and not (full_name or (first_name and last_name)):
        raise ValueError(
            "Provide either linkedin_url OR (full_name or first_name+last_name) "
            "with company/domain."
        )

    item: dict = {}
    if linkedin_url:
        item["profile_url"] = linkedin_url
    else:
        if full_name:
            item["full_name"] = full_name
        else:
            item["first_name"] = first_name
            item["last_name"] = last_name
        if company:
            item["company"] = company
        if domain:
            item["domain"] = domain

    payload = {
        "reveal": {
            "enrichment_level": enrichment_level,
            "email_options": {
                "accept_work": accept_work_email,
                "accept_personal": accept_personal_email,
                "accept_generic": accept_generic_email,
            },
            **item,
        }
    }
    result = _post("/api/reveal", payload)
    return json.dumps(result, indent=2)


if __name__ == "__main__":
    mcp.run()
