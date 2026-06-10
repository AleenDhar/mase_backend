import os
import asyncio
import httpx
from fastmcp import FastMCP

BASE_URL = "https://api.seamless.ai/api/client/v1"
API_KEY = os.getenv("SEAMLESS_API_KEY", "")


def get_headers():
    if not API_KEY:
        raise ValueError("Set SEAMLESS_API_KEY")
    return {"Token": API_KEY, "Content-Type": "application/json"}


async def sl_post(endpoint, payload):
    try:
        h = get_headers()
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{BASE_URL}{endpoint}", headers=h, json=payload, timeout=60)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"error": str(e)}


async def sl_get(endpoint, params=None):
    try:
        h = get_headers()
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{BASE_URL}{endpoint}", headers=h, params=params, timeout=60)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        return {"error": str(e)}


mcp = FastMCP("seamlessai-mcp-server")


@mcp.tool
async def seamless_search_contacts(
    companyName: list[str] = None,
    companyDomain: list[str] = None,
    jobTitle: list[str] = None,
    fullname: list[str] = None,
    department: list[str] = None,
    seniority: list[str] = None,
    industry: list[str] = None,
    contactState: list[str] = None,
    contactCountry: list[str] = None,
    companySize: list[str] = None,
    companyRevenue: list[str] = None,
    technologies: list[str] = None,
    contactKeyword: list[str] = None,
    limit: int = 50,
    nextToken: str = None
):
    """Search Seamless.ai for contacts. Filter by company, title, seniority, department, industry, location, company size/revenue, technologies. Seniority values: C-Level, VP, Director, Manager, Senior, Entry Level, Mid-Level. Department values: Sales, Marketing, Engineering, Human Resources, Finance, IT, Operations, Support, Legal. Company size values: '0 - 1 (Self-employed)', '2 - 10', '11 - 50', '51 - 200', '201 - 500', '501 - 1,000', '1,001 - 5,000', '5,001 - 10,000', '10,001+'. Revenue values: '$0 - $100K', '$100K - $1M', '$1M - $5M', '$5M - $20M', '$20M - $50M', '$50M - $100M', '$100M - $500M', '$500M - $1B', '$1B+'."""
    p = {"limit": limit}
    if nextToken: p["nextToken"] = nextToken
    if companyName: p["companyName"] = companyName
    if companyDomain: p["companyDomain"] = companyDomain
    if jobTitle: p["jobTitle"] = jobTitle
    if fullname: p["fullname"] = fullname
    if department: p["department"] = department
    if seniority: p["seniority"] = seniority
    if industry: p["industry"] = industry
    if contactState: p["contactState"] = contactState
    if contactCountry: p["contactCountry"] = contactCountry
    if companySize: p["companySize"] = companySize
    if companyRevenue: p["companyRevenue"] = companyRevenue
    if technologies: p["technologies"] = technologies
    if contactKeyword: p["contactKeyword"] = contactKeyword
    return await sl_post("/search/contacts", p)


@mcp.tool
async def seamless_search_companies(
    companyName: list[str] = None,
    companyDomain: list[str] = None,
    industry: list[str] = None,
    companyState: list[str] = None,
    companyCountry: list[str] = None,
    companySize: list[str] = None,
    companyRevenue: list[str] = None,
    technologies: list[str] = None,
    companyKeyword: list[str] = None,
    limit: int = 50,
    nextToken: str = None
):
    """Search Seamless.ai for companies. Filter by name, domain, industry, location, size, revenue, technologies. Company size values: '0 - 1 (Self-employed)', '2 - 10', '11 - 50', '51 - 200', '201 - 500', '501 - 1,000', '1,001 - 5,000', '5,001 - 10,000', '10,001+'. Revenue values: '$0 - $100K', '$100K - $1M', '$1M - $5M', '$5M - $20M', '$20M - $50M', '$50M - $100M', '$100M - $500M', '$500M - $1B', '$1B+'."""
    p = {"limit": limit}
    if nextToken: p["nextToken"] = nextToken
    if companyName: p["companyName"] = companyName
    if companyDomain: p["companyDomain"] = companyDomain
    if industry: p["industry"] = industry
    if companyState: p["companyState"] = companyState
    if companyCountry: p["companyCountry"] = companyCountry
    if companySize: p["companySize"] = companySize
    if companyRevenue: p["companyRevenue"] = companyRevenue
    if technologies: p["technologies"] = technologies
    if companyKeyword: p["companyKeyword"] = companyKeyword
    return await sl_post("/search/companies", p)


@mcp.tool
async def seamless_research_contacts(searchResultIds: list[str]):
    """Start async contact research on Seamless.ai. Pass searchResultIds from seamless_search_contacts results. Returns a researchId to poll with seamless_poll_contact_research. This enriches contacts with verified emails, phones, etc."""
    return await sl_post("/research/contacts", {"searchResultIds": searchResultIds})


@mcp.tool
async def seamless_poll_contact_research(researchId: str):
    """Poll for contact research results from Seamless.ai. Use the researchId from seamless_research_contacts. Returns enriched contact data (emails, phones) when ready."""
    return await sl_get("/research/contacts", {"researchId": researchId})


@mcp.tool
async def seamless_research_companies(searchResultIds: list[str]):
    """Start async company research on Seamless.ai. Pass searchResultIds from seamless_search_companies results. Returns a researchId to poll with seamless_poll_company_research."""
    return await sl_post("/research/companies", {"searchResultIds": searchResultIds})


@mcp.tool
async def seamless_poll_company_research(researchId: str):
    """Poll for company research results from Seamless.ai. Use the researchId from seamless_research_companies. Returns enriched company data when ready."""
    return await sl_get("/research/companies", {"researchId": researchId})


@mcp.tool
async def seamless_get_org_contacts(nextToken: str = None, limit: int = 50):
    """Get all contacts saved in your Seamless.ai organization."""
    params = {"limit": limit}
    if nextToken: params["nextToken"] = nextToken
    return await sl_get("/org/contacts", params)


@mcp.tool
async def seamless_get_org_companies(nextToken: str = None, limit: int = 50):
    """Get all companies saved in your Seamless.ai organization."""
    params = {"limit": limit}
    if nextToken: params["nextToken"] = nextToken
    return await sl_get("/org/companies", params)


if __name__ == "__main__":
    mcp.run(transport="stdio")
