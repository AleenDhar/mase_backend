import os
import sys
import json
import asyncio
import time as time_mod
import random
from datetime import datetime, timedelta
import httpx
import jwt
from fastmcp import FastMCP

BASE_URL = "https://api.zoominfo.com"
USERNAME = os.environ.get("ZOOMINFO_USERNAME", "")
CLIENT_ID = os.environ.get("ZOOMINFO_CLIENT_ID", "")
PRIVATE_KEY = os.environ.get("ZOOMINFO_PRIVATE_KEY", "")

_token = None
_token_exp = 0
_auth_lock = None


def _get_lock():
    global _auth_lock
    if _auth_lock is None:
        _auth_lock = asyncio.Lock()
    return _auth_lock


async def get_token():
    global _token, _token_exp
    if _token and time_mod.time() < _token_exp - 120:
        return _token

    async with _get_lock():
        if _token and time_mod.time() < _token_exp - 120:
            return _token

        current_time = datetime.utcnow()
        claims = {
            "aud": "enterprise_api",
            "iss": "api-client@zoominfo.com",
            "iat": current_time,
            "exp": current_time + timedelta(seconds=300),
            "client_id": CLIENT_ID,
            "username": USERNAME,
        }
        client_jwt = jwt.encode(claims, PRIVATE_KEY, algorithm="RS256")

        for attempt in range(4):
            try:
                async with httpx.AsyncClient() as c:
                    r = await c.post(
                        f"{BASE_URL}/authenticate",
                        headers={"Authorization": f"Bearer {client_jwt}", "Accept": "application/json"},
                        timeout=30,
                    )
                    if r.status_code == 429:
                        retry_after = int(r.headers.get("Retry-After", 2 ** (attempt + 1)))
                        wait = retry_after + random.uniform(0.5, 2.0)
                        await asyncio.sleep(wait)
                        continue
                    r.raise_for_status()
                    data = r.json()
                    _token = data["jwt"]
                    _token_exp = time_mod.time() + 3500
                    return _token
            except httpx.HTTPStatusError:
                raise
            except Exception:
                if attempt < 3:
                    await asyncio.sleep(2 ** attempt + random.uniform(0.5, 1.5))
                    continue
                raise

        raise Exception("ZoomInfo auth failed after 4 retries (429 rate limited)")


_request_semaphore = None


def _get_semaphore():
    global _request_semaphore
    if _request_semaphore is None:
        _request_semaphore = asyncio.Semaphore(2)
    return _request_semaphore


async def zi_post(endpoint, payload):
    async with _get_semaphore():
        for attempt in range(3):
            try:
                t = await get_token()
                async with httpx.AsyncClient() as c:
                    r = await c.post(f"{BASE_URL}{endpoint}",
                                     headers={"Authorization": f"Bearer {t}", "Content-Type": "application/json"},
                                     json=payload, timeout=60)
                    if r.status_code == 429:
                        retry_after = int(r.headers.get("Retry-After", 2 ** (attempt + 1)))
                        wait = retry_after + random.uniform(0.5, 2.0)
                        await asyncio.sleep(wait)
                        continue
                    r.raise_for_status()
                    return r.json()
            except httpx.HTTPStatusError as e:
                return {"error": f"HTTP {e.response.status_code}: {e.response.text[:300]}"}
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt + random.uniform(0.5, 1.5))
                    continue
                return {"error": str(e)}
        return {"error": "ZoomInfo request failed after 3 retries (rate limited)"}


mcp = FastMCP("zoominfo-mcp-server")


@mcp.tool
async def zi_search_contacts(
    jobTitle: str = None,
    companyName: str = None,
    state: str = None,
    country: str = None,
    department: str = None,
    managementLevel: str = None,
    revenueMin: int = None,
    revenueMax: int = None,
    employeeCount: str = None,
    industryKeywords: str = None,
    page: int = 1,
    rpp: int = 25
):
    """Search ZoomInfo contacts. managementLevel values: 'C Level Exec', 'VP Level Exec', 'Director', 'Manager', 'Non Manager', 'Board Member'. department uses numeric IDs: 0=C-Suite, 1=Finance, 2=HR, 3=Sales, 4=Operations, 5=IT, 6=Engineering, 7=Marketing, 8=Legal, 9=Medical, 10=Other. employeeCount values: '1to4','5to9','10to19','20to49','50to99','100to249','250to499','500to999','1000to4999','5000to9999','10000plus'. revenueMin/revenueMax in thousands USD (e.g., 1000000 = $1B)."""
    p = {}
    if jobTitle: p["jobTitle"] = jobTitle
    if companyName: p["companyName"] = companyName
    if state: p["state"] = state
    if country: p["country"] = country
    if department: p["department"] = department
    if managementLevel: p["managementLevel"] = managementLevel
    if revenueMin and revenueMin > 0: p["revenueMin"] = revenueMin
    if revenueMax and revenueMax > 0: p["revenueMax"] = revenueMax
    if employeeCount: p["employeeCount"] = employeeCount
    if industryKeywords: p["industryKeywords"] = industryKeywords
    p["page"] = page
    p["rpp"] = rpp
    return await zi_post("/search/contact", p)


@mcp.tool
async def zi_enrich_contact(
    personId: str = None,
    emailAddress: str = None,
    firstName: str = None,
    lastName: str = None,
    companyName: str = None,
    companyDomain: str = None
):
    """Enrich a contact using ZoomInfo by email, name+company, or ZoomInfo person ID."""
    match_input = {k: v for k, v in {
        "personId": personId, "emailAddress": emailAddress,
        "firstName": firstName, "lastName": lastName,
        "companyName": companyName, "companyDomain": companyDomain
    }.items() if v}
    return await zi_post("/enrich/contact", {
        "matchPersonInput": [match_input],
        "outputFields": ["id","firstName","lastName","email","phone","jobTitle",
                         "city","state","country","managementLevel","companyName","companyId","mobilePhone"],
    })


@mcp.tool
async def zi_search_companies(
    companyName: str = None,
    companyWebsite: str = None,
    industryKeywords: str = None,
    revenue: str = None,
    revenueMin: int = None,
    revenueMax: int = None,
    employeeCount: str = None,
    state: str = None,
    country: str = None,
    techAttributeTagList: str = None,
    page: int = 1,
    rpp: int = 25
):
    """Search ZoomInfo companies. revenue enum values: 'under500k','500kto1m','1mto5m','5mto10m','10mto25m','25mto50m','50mto100m','100mmto250m','250mto500m','500mto1g','1gto5g','5gplus'. Or use revenueMin/revenueMax in thousands USD. employeeCount values: '1to4','5to9','10to19','20to49','50to99','100to249','250to499','500to999','1000to4999','5000to9999','10000plus'. industryKeywords supports AND/OR operators."""
    p = {}
    if companyName: p["companyName"] = companyName
    if companyWebsite: p["companyWebsite"] = companyWebsite
    if industryKeywords: p["industryKeywords"] = industryKeywords
    if revenue: p["revenue"] = revenue
    if revenueMin and revenueMin > 0 and not revenue: p["revenueMin"] = revenueMin
    if revenueMax and revenueMax > 0 and not revenue: p["revenueMax"] = revenueMax
    if employeeCount: p["employeeCount"] = employeeCount
    if state: p["state"] = state
    if country: p["country"] = country
    if techAttributeTagList: p["techAttributeTagList"] = techAttributeTagList
    p["page"] = page
    p["rpp"] = rpp
    return await zi_post("/search/company", p)


@mcp.tool
async def zi_enrich_company(
    companyId: str = None,
    companyName: str = None,
    companyDomain: str = None
):
    """Enrich a company using ZoomInfo by website/domain, name, or ZoomInfo company ID."""
    match_input = {k: v for k, v in {
        "companyId": companyId, "companyName": companyName, "companyWebsite": companyDomain
    }.items() if v}
    return await zi_post("/enrich/company", {
        "matchCompanyInput": [match_input],
        "outputFields": ["id","name","website","revenue","employeeCount",
                         "city","state","country","foundedYear",
                         "ticker","phone","street","zipCode"],
    })


@mcp.tool
async def zi_get_scoops(
    scoopTopic: str = None,
    companyId: str = None,
    companyName: str = None,
    companyWebsite: str = None,
    publishedStartDate: str = None,
    publishedEndDate: str = None,
    country: str = None,
    page: int = 1,
    rpp: int = 25
):
    """Search ZoomInfo Scoops (buying signals, leadership changes, expansions, funding). scoopTopic is a comma-separated list of topic IDs. Key topic IDs: 14=Executive Moves, 17=New Hire, 18=Promotion, 19=Seeking Replacement, 26=Mergers & Acquisitions, 33=Hiring Plans, 34=Facilities Expansion, 41=Spending/Investment, 117=Funding, 107=Layoffs. Dates use YYYY-MM-DD format."""
    p = {"rpp": rpp, "page": page}
    if scoopTopic: p["scoopTopic"] = scoopTopic
    if companyId: p["companyId"] = companyId
    if companyName: p["companyName"] = companyName
    if companyWebsite: p["companyWebsite"] = companyWebsite
    if publishedStartDate: p["publishedStartDate"] = publishedStartDate
    if publishedEndDate: p["publishedEndDate"] = publishedEndDate
    if country: p["country"] = country
    return await zi_post("/search/scoop", p)


@mcp.tool
async def zi_get_intent(
    topics: list[str] = None,
    audienceStrengthMin: str = None,
    audienceStrengthMax: str = None,
    signalScoreMin: int = None,
    country: str = None,
    page: int = 1,
    rpp: int = 25
):
    """Get ZoomInfo intent signals for companies researching topics. IMPORTANT: topics is REQUIRED - must be an Array of topic name strings from the account's subscribed intent topics (use zi_list_intent_topics to see available topics). audienceStrength values: A (highest), B, C, D, E (lowest) - audienceStrengthMin should be WEAKER (e.g. C) and audienceStrengthMax should be STRONGER (e.g. A). signalScoreMin: 60-100. NOTE: Company-level filters (companyId, companyWebsite) are NOT supported on this account - to find intent for a specific company, retrieve all intent results and filter client-side by company name."""
    p = {"rpp": rpp, "page": page}
    if topics: p["topics"] = topics
    if audienceStrengthMin: p["audienceStrengthMin"] = audienceStrengthMin
    if audienceStrengthMax: p["audienceStrengthMax"] = audienceStrengthMax
    if signalScoreMin and signalScoreMin > 0: p["signalScoreMin"] = signalScoreMin
    if country: p["country"] = country
    return await zi_post("/search/intent", p)


@mcp.tool
async def zi_list_intent_topics():
    """List all intent topics the account is subscribed to. Use these topic names with zi_get_intent."""
    async with _get_semaphore():
        try:
            t = await get_token()
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{BASE_URL}/lookup/intent/topics",
                                headers={"Authorization": f"Bearer {t}"},
                                timeout=30)
                r.raise_for_status()
                return r.json()
        except Exception as e:
            return {"error": str(e)}


@mcp.tool
async def zi_get_news(
    companyId: str = None,
    companyName: str = None,
    companyWebsite: str = None,
    pageDateMin: str = None,
    pageDateMax: str = None,
    page: int = 1,
    rpp: int = 25
):
    """Get company news articles from ZoomInfo. Dates use YYYY-MM-DD format. WARNING: This endpoint returns 403 Forbidden because the account does NOT have News API entitlement. Contact ZoomInfo Account Manager to enable. Use duckduckgo_search or apollo_search_news_articles as alternatives for company news."""
    p = {"rpp": rpp, "page": page}
    if companyId: p["companyId"] = companyId
    if companyName: p["companyName"] = companyName
    if companyWebsite: p["companyWebsite"] = companyWebsite
    if pageDateMin: p["pageDateMin"] = pageDateMin
    if pageDateMax: p["pageDateMax"] = pageDateMax
    return await zi_post("/search/news", p)


@mcp.tool
async def zi_get_technologies(companyId: str = None):
    """Get the technology stack used by a company via ZoomInfo. Requires companyId (get from zi_search_companies or zi_enrich_company first)."""
    p = {}
    if companyId: p["companyId"] = companyId
    return await zi_post("/enrich/tech", p)


@mcp.tool
async def zi_get_org_chart(companyId: str = None):
    """Get org chart / reporting hierarchy for a company via ZoomInfo. Requires companyId."""
    p = {}
    if companyId: p["companyId"] = companyId
    return await zi_post("/enrich/orgchart", p)


if __name__ == "__main__":
    mcp.run(transport="stdio")
