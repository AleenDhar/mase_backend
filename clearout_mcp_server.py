import os
import json
from typing import Optional

import httpx
from fastmcp import FastMCP

CLEAROUT_BASE = "https://api.clearout.io/v2"
CLEAROUT_PUBLIC_BASE = "https://api.clearout.io"
API_KEY = os.environ.get("CLEAROUT_API_KEY", "").strip()

mcp = FastMCP(
    name="Clearout",
    instructions=(
        "Use this server to verify email addresses, find emails for contacts, "
        "perform reverse lookups (email, LinkedIn, domain), look up MX and WHOIS records, "
        "and autocomplete company domain discovery. "
        "Requires CLEAROUT_API_KEY environment variable. "
        "All endpoints use Bearer token authentication."
    ),
)


def _headers() -> dict:
    if not API_KEY:
        raise RuntimeError(
            "CLEAROUT_API_KEY environment variable is not set. "
            "Generate your API token from the Clearout dashboard under Developer → API."
        )
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }


def _post(endpoint: str, payload: dict) -> dict:
    url = f"{CLEAROUT_BASE}{endpoint}"
    with httpx.Client(timeout=120) as client:
        resp = client.post(url, headers=_headers(), json=payload)
    if resp.status_code >= 400:
        raise RuntimeError(f"Clearout API error {resp.status_code}: {resp.text}")
    return resp.json()


def _get(endpoint: str, params: dict = None, base: str = None) -> dict:
    url = f"{base or CLEAROUT_BASE}{endpoint}"
    with httpx.Client(timeout=60) as client:
        resp = client.get(url, headers=_headers(), params=params)
    if resp.status_code >= 400:
        raise RuntimeError(f"Clearout API error {resp.status_code}: {resp.text}")
    return resp.json()


def _get_public(endpoint: str, params: dict = None, base: str = None) -> dict:
    """GET without auth headers — for Clearout's free public endpoints."""
    url = f"{base or CLEAROUT_PUBLIC_BASE}{endpoint}"
    with httpx.Client(timeout=60) as client:
        resp = client.get(url, params=params)
    if resp.status_code >= 400:
        raise RuntimeError(f"Clearout API error {resp.status_code}: {resp.text}")
    return resp.json()


@mcp.tool()
def clearout_verify_email(
    email: str,
    timeout_ms: int = 15000,
) -> str:
    """
    Instantly verify a single email address for deliverability and validity.

    Args:
        email: The email address to verify (e.g. "john@example.com").
        timeout_ms: Max wait time in milliseconds (default 15000, max 180000).

    Returns:
        JSON with status (safe/unsafe/unknown), deliverability, disposable flag,
        role-based flag, MX records found, and detailed sub-status.
    """
    payload = {"email": email, "timeout": timeout_ms}
    result = _post("/email_verify/instant", payload)
    return json.dumps({"status": "success", "result": result}, indent=2, default=str)


@mcp.tool()
def clearout_find_email(
    name: str,
    domain: str,
    timeout_ms: int = 20000,
    background: bool = True,
) -> str:
    """
    Find a verified business email address for a person given their name and company/domain.

    Args:
        name: Full name of the person (e.g. "Tony Stark" or "Mr. Robert Downey Jr.").
        domain: Company domain or company name (e.g. "marvel.com" or "Marvel Entertainment").
        timeout_ms: Max wait time in milliseconds (default 20000).
        background: If True, discovery continues in background after timeout and
            result can be retrieved via clearout_get_finder_status. Default True.

    Returns:
        JSON with discovered email address, confidence score, and queue_id if still processing.
    """
    payload = {
        "name": name,
        "domain": domain,
        "timeout": timeout_ms,
        "background": background,
    }
    result = _post("/email_finder/instant", payload)
    return json.dumps({"status": "success", "result": result}, indent=2, default=str)


@mcp.tool()
def clearout_get_finder_status(queue_id: str) -> str:
    """
    Check the status of an email finder request that is still processing in the queue.

    Args:
        queue_id: The queue_id returned by clearout_find_email when still processing.

    Returns:
        JSON with current status and discovered email address if ready.
    """
    result = _get("/email_finder/instant/queue_status", params={"queue_id": queue_id})
    return json.dumps({"status": "success", "result": result}, indent=2, default=str)


@mcp.tool()
def clearout_reverse_lookup_email(email_address: str) -> str:
    """
    Retrieve lead information (name, company, LinkedIn, job title, etc.)
    from an email address using reverse lookup.

    Args:
        email_address: The email address to look up (e.g. "bill@microsoft.com").

    Returns:
        JSON with contact details: name, company, title, LinkedIn URL, and more.
    """
    result = _get("/reverse_lookup/email", params={"email_address": email_address})
    return json.dumps({"status": "success", "result": result}, indent=2, default=str)


@mcp.tool()
def clearout_reverse_lookup_linkedin(linkedin_url: str) -> str:
    """
    Retrieve lead information from a LinkedIn profile URL using reverse lookup.

    Args:
        linkedin_url: The LinkedIn profile URL
            (e.g. "https://www.linkedin.com/in/williamhgates/").

    Returns:
        JSON with contact details: email, name, company, title, and more.
    """
    result = _get("/reverse_lookup/linkedin", params={"url": linkedin_url})
    return json.dumps({"status": "success", "result": result}, indent=2, default=str)


@mcp.tool()
def clearout_reverse_lookup_domain(domain: str) -> str:
    """
    Retrieve company information from a domain name using reverse lookup.

    Args:
        domain: The domain name to look up (e.g. "microsoft.com").

    Returns:
        JSON with company details: name, industry, size, location, LinkedIn, and more.
    """
    result = _get("/reverse_lookup/domain", params={"name": domain})
    return json.dumps({"status": "success", "result": result}, indent=2, default=str)


@mcp.tool()
def clearout_company_autocomplete(query: str) -> str:
    """
    Find the website domain for a company name using Clearout's free autocomplete API.
    Returns matching company domains with logos and confidence scores.

    Args:
        query: A company name, domain, or website URL (e.g. "Amazon" or "amazon").

    Returns:
        JSON list of matching companies with name, domain, and logo_url.
    """
    result = _get_public(
        "/public/companies/autocomplete",
        params={"query": query},
    )
    return json.dumps({"status": "success", "result": result}, indent=2, default=str)


@mcp.tool()
def clearout_get_mx_records(domain: str, timeout_ms: int = 10000) -> str:
    """
    Retrieve MX (mail exchange) records for a domain to check if it can receive email.

    Args:
        domain: The domain to look up MX records for (e.g. "gmail.com").
        timeout_ms: Max wait time in milliseconds (default 10000, max 110000).

    Returns:
        JSON with MX record details for the domain.
    """
    payload = {"domain": domain, "timeout": timeout_ms}
    result = _post("/domain/resolve/mx", payload)
    return json.dumps({"status": "success", "result": result}, indent=2, default=str)


@mcp.tool()
def clearout_get_whois(domain: str, timeout_ms: int = 10000) -> str:
    """
    Retrieve the WHOIS record for a domain in structured JSON format.
    Useful for finding domain registrant info, creation date, and registrar.

    Args:
        domain: The domain to look up (e.g. "apple.com").
        timeout_ms: Max wait time in milliseconds (default 10000, max 110000).

    Returns:
        JSON with WHOIS data: registrant, registrar, creation date, expiry, nameservers.
    """
    payload = {"domain": domain, "timeout": timeout_ms}
    result = _post("/domain/resolve/whois", payload)
    return json.dumps({"status": "success", "result": result}, indent=2, default=str)


if __name__ == "__main__":
    mcp.run(transport="stdio")
