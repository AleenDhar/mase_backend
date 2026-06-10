"""
Lusha MCP Server
================
Exposes Lusha's API categories as MCP tools:
  - Enrichment  (person & company, single + bulk)
  - Prospecting (contact & company search)
  - Signals     (contact & company)
  - Lookalikes  (contact & company)
  - Account     (credit usage)

Authentication: Set LUSHA_API_KEY environment variable.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

LUSHA_BASE_URL = "https://api.lusha.com"
API_KEY = os.environ.get("LUSHA_API_KEY", "")
DEFAULT_TIMEOUT = 30.0
MAX_BULK = 100


def _headers() -> Dict[str, str]:
    if not API_KEY:
        raise RuntimeError(
            "LUSHA_API_KEY environment variable is not set. "
            "Export it before starting the server."
        )
    return {"api_key": API_KEY, "Content-Type": "application/json"}


async def lusha_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        r = await client.get(f"{LUSHA_BASE_URL}{path}", headers=_headers(), params=params)
        r.raise_for_status()
        return r.json()


async def lusha_post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
        r = await client.post(f"{LUSHA_BASE_URL}{path}", headers=_headers(), json=body)
        r.raise_for_status()
        return r.json()


def _ok(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


def _handle_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text
        messages = {
            401: "Authentication failed. Check your LUSHA_API_KEY.",
            403: "Access denied. Your plan may not support this feature (e.g. revealEmails/revealPhones require Unified Credits).",
            404: "Resource not found. Verify the identifiers you supplied.",
            429: "Rate limit exceeded (25 req/s). Wait a moment and retry.",
        }
        hint = messages.get(status, f"HTTP {status} error.")
        return json.dumps({"error": hint, "detail": detail}, indent=2)
    if isinstance(e, httpx.TimeoutException):
        return json.dumps({"error": "Request timed out. Please retry."})
    return json.dumps({"error": f"Unexpected error: {type(e).__name__}: {e}"})


mcp = FastMCP("lusha_mcp")


class EnrichPersonInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    linkedin_url: Optional[str] = Field(
        default=None,
        description="LinkedIn profile URL, e.g. 'https://linkedin.com/in/johndoe'. "
                    "Preferred identifier – supply at least one of: linkedin_url, email, "
                    "or (first_name + last_name + company_domain).",
    )
    email: Optional[str] = Field(
        default=None,
        description="Work email address, e.g. 'john@acme.com'.",
    )
    first_name: Optional[str] = Field(default=None, description="Contact's first name.")
    last_name: Optional[str] = Field(default=None, description="Contact's last name.")
    company_name: Optional[str] = Field(default=None, description="Employer company name.")
    company_domain: Optional[str] = Field(
        default=None, description="Employer domain, e.g. 'acme.com'."
    )
    reveal_emails: bool = Field(
        default=False,
        description="Consume credits to reveal verified email addresses (requires Unified Credits plan).",
    )
    reveal_phones: bool = Field(
        default=False,
        description="Consume credits to reveal phone numbers (requires Unified Credits plan).",
    )
    include_signals: bool = Field(
        default=False,
        description="Include recent career-change signals (job moves, promotions) in the response.",
    )
    signals_start_date: Optional[str] = Field(
        default=None,
        description="ISO date (YYYY-MM-DD) from which to return signals. Only used when include_signals=true.",
    )


@mcp.tool(name="lusha_enrich_person")
async def lusha_enrich_person(params: EnrichPersonInput) -> str:
    """Enrich a single contact with verified emails, phone numbers, job title,
    seniority, department, and firmographics from Lusha's database.

    Supply at least one identifier: linkedin_url, email, or
    (first_name + last_name + company_domain / company_name).
    """
    query: Dict[str, Any] = {}
    if params.linkedin_url:
        query["linkedinUrl"] = params.linkedin_url
    if params.email:
        query["email"] = params.email
    if params.first_name:
        query["firstName"] = params.first_name
    if params.last_name:
        query["lastName"] = params.last_name
    if params.company_name:
        query["companyName"] = params.company_name
    if params.company_domain:
        query["companyDomain"] = params.company_domain
    if params.reveal_emails:
        query["revealEmails"] = True
    if params.reveal_phones:
        query["revealPhones"] = True
    if params.include_signals:
        query["signals"] = True
    if params.signals_start_date:
        query["signalsStartDate"] = params.signals_start_date

    try:
        data = await lusha_get("/v2/person", params=query)
        return _ok(data)
    except Exception as e:
        return _handle_error(e)


class BulkPersonItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    linkedin_url: Optional[str] = Field(default=None, description="LinkedIn profile URL.")
    email: Optional[str] = Field(default=None, description="Work email address.")
    first_name: Optional[str] = Field(default=None)
    last_name: Optional[str] = Field(default=None)
    company_name: Optional[str] = Field(default=None)
    company_domain: Optional[str] = Field(default=None)


class BulkEnrichPersonInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contacts: List[BulkPersonItem] = Field(
        ...,
        description=f"List of contacts to enrich (max {MAX_BULK}).",
        max_length=MAX_BULK,
    )
    reveal_emails: bool = Field(default=False)
    reveal_phones: bool = Field(default=False)


@mcp.tool(name="lusha_bulk_enrich_persons")
async def lusha_bulk_enrich_persons(params: BulkEnrichPersonInput) -> str:
    """Enrich up to 100 contacts in a single API call.

    Each contact object requires at least one identifier (linkedin_url, email,
    or name + company). Results are returned as a list aligned with input order.
    """
    payload: Dict[str, Any] = {
        "contacts": [
            {k: v for k, v in {
                "linkedinUrl": c.linkedin_url,
                "email": c.email,
                "firstName": c.first_name,
                "lastName": c.last_name,
                "company": c.company_name,
                "companyDomain": c.company_domain,
            }.items() if v is not None}
            for c in params.contacts
        ]
    }
    if params.reveal_emails:
        payload["revealEmails"] = True
    if params.reveal_phones:
        payload["revealPhones"] = True

    try:
        data = await lusha_post("/bulk/person/v2", payload)
        return _ok(data)
    except Exception as e:
        return _handle_error(e)


class EnrichCompanyInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    domain: Optional[str] = Field(
        default=None,
        description="Company domain, e.g. 'stripe.com'. Preferred identifier.",
    )
    company_name: Optional[str] = Field(
        default=None,
        description="Company name. Used when domain is unknown.",
    )
    company_id: Optional[str] = Field(
        default=None,
        description="Lusha internal company ID (from a previous enrichment).",
    )


@mcp.tool(name="lusha_enrich_company")
async def lusha_enrich_company(params: EnrichCompanyInput) -> str:
    """Enrich a company with firmographics: size, industry, revenue, HQ location,
    technology stack, and more.

    Supply at least one of: domain, company_name, or company_id.
    """
    query: Dict[str, Any] = {}
    if params.domain:
        query["domain"] = params.domain
    if params.company_name:
        query["company"] = params.company_name
    if params.company_id:
        query["companyId"] = params.company_id

    try:
        data = await lusha_get("/v2/company", params=query)
        return _ok(data)
    except Exception as e:
        return _handle_error(e)


class BulkCompanyItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    domain: Optional[str] = Field(default=None, description="Company domain.")
    company_name: Optional[str] = Field(default=None, description="Company name.")
    company_id: Optional[str] = Field(default=None, description="Lusha company ID.")


class BulkEnrichCompanyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    companies: List[BulkCompanyItem] = Field(
        ...,
        description=f"List of companies to enrich (max {MAX_BULK}).",
        max_length=MAX_BULK,
    )


@mcp.tool(name="lusha_bulk_enrich_companies")
async def lusha_bulk_enrich_companies(params: BulkEnrichCompanyInput) -> str:
    """Enrich up to 100 companies in a single request."""
    payload = {
        "companies": [
            {k: v for k, v in {
                "domain": c.domain,
                "company": c.company_name,
                "companyId": c.company_id,
            }.items() if v is not None}
            for c in params.companies
        ]
    }
    try:
        data = await lusha_post("/bulk/company/v2", payload)
        return _ok(data)
    except Exception as e:
        return _handle_error(e)


# ---------------------------------------------------------------------------
# Prospecting v3 — POST /v3/{contacts|companies}/prospecting
# Docs: https://docs.lusha.com/apis/openapi/prospecting/
# Key differences from the deprecated v2 prospecting endpoints:
#   - envelope is `pagination: {page, size}` (was `pages` then `limit/offset`)
#   - max `size` is 50; max `page` is 999 (so up to 50,000 results per query)
#   - filters live under `filters.contacts.include` and `filters.companies.include`
#   - seniority + industry use numeric IDs (look them up via
#     lusha_get_filter_values before searching)
#   - `sizes` is a list of {min, max} bands, not strings
#   - locations are objects with city/state/country/continent/countryGrouping
#   - response uses `results[]` and `pagination.total`
# ---------------------------------------------------------------------------

PROSPECTING_MAX_PAGE_SIZE = 50


class LocationFilter(BaseModel):
    """Geographic filter — supply at least one of city/state/country/continent/countryGrouping."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    city: Optional[str] = Field(default=None, description="e.g. 'San Francisco'.")
    state: Optional[str] = Field(default=None, description="e.g. 'California'.")
    country: Optional[str] = Field(default=None, description="e.g. 'United States'.")
    continent: Optional[str] = Field(default=None, description="e.g. 'North America'.")
    country_grouping: Optional[str] = Field(
        default=None, description="Regional grouping, e.g. 'EMEA', 'APAC'."
    )

    def to_lusha(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if self.city: out["city"] = self.city
        if self.state: out["state"] = self.state
        if self.country: out["country"] = self.country
        if self.continent: out["continent"] = self.continent
        if self.country_grouping: out["countryGrouping"] = self.country_grouping
        return out


class SizeBand(BaseModel):
    """Headcount band, e.g. {min: 201, max: 500}. `max` omitted = 'min and up'."""
    model_config = ConfigDict(extra="forbid")
    min: int = Field(..., ge=1)
    max: Optional[int] = Field(default=None, ge=1)


class ProspectContactSearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    # --- contact-level filters (filters.contacts.include.*) ---
    job_titles: Optional[List[str]] = Field(
        default=None, description="Job titles, e.g. ['VP Sales', 'Head of Marketing']."
    )
    job_titles_exact_match: Optional[List[str]] = Field(
        default=None, description="Exact-match job titles (no fuzzy expansion)."
    )
    departments: Optional[List[str]] = Field(
        default=None,
        description="Departments. Use lusha_get_filter_values('contact','departments') for valid values.",
    )
    seniority_ids: Optional[List[int]] = Field(
        default=None,
        description="Numeric seniority IDs. Use lusha_get_filter_values('contact','seniority') to look up IDs.",
    )
    countries: Optional[List[str]] = Field(
        default=None, description="ISO country codes, e.g. ['US','CA']."
    )
    locations: Optional[List[LocationFilter]] = Field(
        default=None,
        description="Structured locations. Each entry is an object with city/state/country/continent/countryGrouping.",
    )
    existing_data_points: Optional[List[str]] = Field(
        default=None,
        description="Require contacts to already have certain data, e.g. ['work_email','work_phone'].",
    )
    linkedin_urls: Optional[List[str]] = Field(
        default=None, description="Filter to specific LinkedIn URLs."
    )

    # --- company-level filters (filters.companies.include.*) ---
    company_names: Optional[List[str]] = Field(
        default=None, description="Restrict search to specific employer names."
    )
    company_domains: Optional[List[str]] = Field(
        default=None, description="Restrict search to specific employer domains."
    )
    company_ids: Optional[List[str]] = Field(
        default=None, description="Restrict search to specific Lusha company IDs."
    )
    main_industries_ids: Optional[List[int]] = Field(
        default=None,
        description="Numeric main-industry IDs. Use lusha_get_filter_values('company','industriesLabels') for IDs.",
    )
    sub_industries_ids: Optional[List[int]] = Field(
        default=None, description="Numeric sub-industry IDs."
    )
    company_sizes: Optional[List[SizeBand]] = Field(
        default=None,
        description="Headcount bands as {min,max} objects, e.g. [{'min':201,'max':500}].",
    )
    company_locations: Optional[List[LocationFilter]] = Field(
        default=None, description="Company HQ locations as structured objects."
    )
    technologies: Optional[List[str]] = Field(
        default=None,
        description="Technologies in use. Use lusha_get_filter_values('company','technologies', query='salesforce') to discover.",
    )
    intent_topics: Optional[List[str]] = Field(default=None, description="Intent topic names.")

    # --- options + pagination ---
    max_contacts_per_company: Optional[int] = Field(
        default=None, ge=1, description="Cap how many contacts come from any single company."
    )
    include_partial_profiles: bool = Field(
        default=False, description="Include profiles missing some core fields."
    )
    exclude_dnc: bool = Field(
        default=False, description="Exclude contacts on Do-Not-Call lists."
    )
    limit: int = Field(
        default=25, ge=1, le=PROSPECTING_MAX_PAGE_SIZE,
        description=f"Page size (1-{PROSPECTING_MAX_PAGE_SIZE}). Lusha v3 caps at {PROSPECTING_MAX_PAGE_SIZE}.",
    )
    offset: int = Field(
        default=0, ge=0,
        description="Pagination offset. Will be converted to page = offset // limit.",
    )


def _build_contacts_include(p: ProspectContactSearchInput) -> Dict[str, Any]:
    inc: Dict[str, Any] = {}
    if p.job_titles: inc["jobTitles"] = p.job_titles
    if p.job_titles_exact_match: inc["jobTitlesExactMatch"] = p.job_titles_exact_match
    if p.departments: inc["departments"] = p.departments
    if p.seniority_ids: inc["seniorityIds"] = p.seniority_ids
    if p.countries: inc["countries"] = p.countries
    if p.locations:
        inc["locations"] = [loc.to_lusha() for loc in p.locations]
    if p.existing_data_points: inc["existingDataPoints"] = p.existing_data_points
    if p.linkedin_urls: inc["linkedinUrls"] = p.linkedin_urls
    return inc


def _build_companies_include_for_contacts(p: ProspectContactSearchInput) -> Dict[str, Any]:
    inc: Dict[str, Any] = {}
    if p.company_names: inc["names"] = p.company_names
    if p.company_domains: inc["domains"] = p.company_domains
    if p.company_ids: inc["ids"] = p.company_ids
    if p.main_industries_ids: inc["mainIndustriesIds"] = p.main_industries_ids
    if p.sub_industries_ids: inc["subIndustriesIds"] = p.sub_industries_ids
    if p.company_sizes:
        inc["sizes"] = [s.model_dump(exclude_none=True) for s in p.company_sizes]
    if p.company_locations:
        inc["locations"] = [loc.to_lusha() for loc in p.company_locations]
    if p.technologies: inc["technologies"] = p.technologies
    if p.intent_topics: inc["intentTopics"] = p.intent_topics
    return inc


@mcp.tool(name="lusha_prospect_contacts")
async def lusha_prospect_contacts(params: ProspectContactSearchInput) -> str:
    """Search Lusha v3 for net-new contacts matching your ICP.

    Filters split into:
      - contact attributes: job_titles, departments, seniority_ids, countries,
        locations, existing_data_points, linkedin_urls
      - employer attributes: company_names, company_domains, company_ids,
        main_industries_ids, sub_industries_ids, company_sizes, company_locations,
        technologies, intent_topics

    Use lusha_list_prospecting_filters + lusha_get_filter_values first to
    resolve string labels (e.g. 'Software') to the integer IDs Lusha requires.

    Returns id values you can then pass to lusha_enrich_person to reveal contact info.
    """
    if params.offset % params.limit != 0:
        return json.dumps({
            "error": (
                "`offset` must be a multiple of `limit` (Lusha pages are fixed-size). "
                f"Got offset={params.offset}, limit={params.limit}. "
                f"Use offset=0, {params.limit}, {params.limit * 2}, ..."
            )
        })
    page = params.offset // params.limit
    body: Dict[str, Any] = {"pagination": {"page": page, "size": params.limit}}

    filters: Dict[str, Any] = {}
    contacts_include = _build_contacts_include(params)
    if contacts_include:
        filters["contacts"] = {"include": contacts_include}
    companies_include = _build_companies_include_for_contacts(params)
    if companies_include:
        filters["companies"] = {"include": companies_include}
    if filters:
        body["filters"] = filters

    options: Dict[str, Any] = {}
    if params.max_contacts_per_company is not None:
        options["maxContactsPerCompany"] = params.max_contacts_per_company
    if params.include_partial_profiles:
        options["includePartialProfiles"] = True
    if params.exclude_dnc:
        options["excludeDnc"] = True
    if options:
        body["options"] = options

    try:
        data = await lusha_post("/v3/contacts/prospecting", body)
        results = data.get("results", [])
        pag = data.get("pagination", {}) or {}
        total = pag.get("total", len(results))
        returned = len(results)
        result = {
            "total": total,
            "count": returned,
            "page": pag.get("page", page),
            "size": pag.get("size", params.limit),
            "offset": params.offset,
            "has_more": total > params.offset + returned,
            "next_offset": params.offset + returned if total > params.offset + returned else None,
            "request_id": data.get("requestId"),
            "contacts": results,
        }
        return _ok(result)
    except Exception as e:
        return _handle_error(e)


class ProspectCompanySearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    # --- company.include.* ---
    names: Optional[List[str]] = Field(default=None, description="Company names.")
    domains: Optional[List[str]] = Field(default=None, description="Company domains.")
    ids: Optional[List[str]] = Field(default=None, description="Lusha company IDs.")
    search_text: Optional[str] = Field(default=None, description="Free-text search on company.")
    locations: Optional[List[LocationFilter]] = Field(
        default=None, description="HQ locations as structured objects."
    )
    sizes: Optional[List[SizeBand]] = Field(
        default=None,
        description="Headcount bands as {min,max} objects. Use lusha_get_filter_values('company','sizes') for presets.",
    )
    revenues: Optional[List[str]] = Field(
        default=None,
        description="Revenue band identifiers. Use lusha_get_filter_values('company','revenues') to discover.",
    )
    main_industries_ids: Optional[List[int]] = Field(
        default=None,
        description="Numeric main-industry IDs. Use lusha_get_filter_values('company','industriesLabels').",
    )
    sub_industries_ids: Optional[List[int]] = Field(default=None, description="Numeric sub-industry IDs.")
    technologies: Optional[List[str]] = Field(
        default=None,
        description="Technologies. Use lusha_get_filter_values('company','technologies', query='...').",
    )
    technologies_condition: Optional[str] = Field(
        default=None, description="'and' = all techs required; 'or' = any tech matches."
    )
    intent_topics: Optional[List[str]] = Field(default=None, description="Buyer intent topics.")
    intent_topics_condition: Optional[str] = Field(
        default=None, description="'and' = all topics required; 'or' = any topic matches."
    )
    sic_codes: Optional[List[str]] = Field(default=None, description="SIC industry classification codes.")
    naics_codes: Optional[List[str]] = Field(default=None, description="NAICS industry classification codes.")

    # --- options + pagination ---
    include_partial_profiles: bool = Field(default=False)
    limit: int = Field(
        default=25, ge=1, le=PROSPECTING_MAX_PAGE_SIZE,
        description=f"Page size (1-{PROSPECTING_MAX_PAGE_SIZE}).",
    )
    offset: int = Field(default=0, ge=0)


@mcp.tool(name="lusha_prospect_companies")
async def lusha_prospect_companies(params: ProspectCompanySearchInput) -> str:
    """Search Lusha v3 for companies matching your target market.

    Use lusha_get_filter_values to resolve labels into IDs Lusha requires
    (e.g. industriesLabels → main_industries_ids).
    """
    if params.offset % params.limit != 0:
        return json.dumps({
            "error": (
                "`offset` must be a multiple of `limit` (Lusha pages are fixed-size). "
                f"Got offset={params.offset}, limit={params.limit}. "
                f"Use offset=0, {params.limit}, {params.limit * 2}, ..."
            )
        })
    page = params.offset // params.limit
    body: Dict[str, Any] = {"pagination": {"page": page, "size": params.limit}}

    inc: Dict[str, Any] = {}
    if params.names: inc["names"] = params.names
    if params.domains: inc["domains"] = params.domains
    if params.ids: inc["ids"] = params.ids
    if params.search_text: inc["searchText"] = params.search_text
    if params.locations:
        inc["locations"] = [loc.to_lusha() for loc in params.locations]
    if params.sizes:
        inc["sizes"] = [s.model_dump(exclude_none=True) for s in params.sizes]
    if params.revenues: inc["revenues"] = params.revenues
    if params.main_industries_ids: inc["mainIndustriesIds"] = params.main_industries_ids
    if params.sub_industries_ids: inc["subIndustriesIds"] = params.sub_industries_ids
    if params.technologies: inc["technologies"] = params.technologies
    if params.technologies_condition: inc["technologiesCondition"] = params.technologies_condition
    if params.intent_topics: inc["intentTopics"] = params.intent_topics
    if params.intent_topics_condition: inc["intentTopicsCondition"] = params.intent_topics_condition
    if params.sic_codes: inc["sicCodes"] = params.sic_codes
    if params.naics_codes: inc["naicsCodes"] = params.naics_codes

    if not inc:
        return json.dumps({
            "error": (
                "lusha_prospect_companies requires at least one filter. "
                "Supply some combination of: main_industries_ids, sizes, locations, "
                "technologies, revenues, names, domains, search_text, etc. "
                "Use lusha_list_prospecting_filters(entity='company') to see options."
            )
        })
    body["filters"] = {"companies": {"include": inc}}

    if params.include_partial_profiles:
        body["options"] = {"includePartialProfiles": True}

    try:
        data = await lusha_post("/v3/companies/prospecting", body)
        results = data.get("results", [])
        pag = data.get("pagination", {}) or {}
        total = pag.get("total", len(results))
        returned = len(results)
        result = {
            "total": total,
            "count": returned,
            "page": pag.get("page", page),
            "size": pag.get("size", params.limit),
            "offset": params.offset,
            "has_more": total > params.offset + returned,
            "next_offset": params.offset + returned if total > params.offset + returned else None,
            "request_id": data.get("requestId"),
            "companies": results,
        }
        return _ok(result)
    except Exception as e:
        return _handle_error(e)


# ---------------------------------------------------------------------------
# Prospecting filter discovery — required before most v3 prospecting searches.
# ---------------------------------------------------------------------------

_PROSPECT_ENTITIES = {"contact": "contacts", "company": "companies"}


class ListProspectingFiltersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    entity: str = Field(
        ...,
        description="'contact' or 'company' — which prospecting filter menu to list.",
    )


@mcp.tool(name="lusha_list_prospecting_filters")
async def lusha_list_prospecting_filters(params: ListProspectingFiltersInput) -> str:
    """List the available filter types for a Lusha v3 prospecting search.

    Call this first when you don't know which filters Lusha exposes for a
    given entity. Returns a menu of filter types and whether each one needs
    a `query` parameter when calling lusha_get_filter_values.
    """
    seg = _PROSPECT_ENTITIES.get(params.entity.lower())
    if not seg:
        return json.dumps({"error": "entity must be 'contact' or 'company'."})
    try:
        data = await lusha_get(f"/v3/{seg}/prospecting/filters")
        return _ok(data)
    except Exception as e:
        return _handle_error(e)


class GetFilterValuesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    entity: str = Field(..., description="'contact' or 'company'.")
    filter_type: str = Field(
        ...,
        description="Filter type from lusha_list_prospecting_filters, e.g. 'seniority', 'industriesLabels', 'sizes', 'technologies'.",
    )
    query: Optional[str] = Field(
        default=None,
        description="Search query — required for filter types that have `requiresQuery: true` (e.g. 'locations', 'technologies', 'names').",
    )


@mcp.tool(name="lusha_get_filter_values")
async def lusha_get_filter_values(params: GetFilterValuesInput) -> str:
    """Get the valid values (and IDs) for a specific Lusha v3 prospecting filter.

    Call this to translate human labels into the IDs Lusha expects, e.g.:
      - filter_type='seniority' returns seniority IDs to use as seniority_ids
      - filter_type='industriesLabels' returns main_industry_id + sub_industries
      - filter_type='sizes' returns the preset {min,max} bands
      - filter_type='technologies' (needs query) returns matching tech names

    Some filter types require a `query` — check requiresQuery from
    lusha_list_prospecting_filters first.
    """
    seg = _PROSPECT_ENTITIES.get(params.entity.lower())
    if not seg:
        return json.dumps({"error": "entity must be 'contact' or 'company'."})
    try:
        qs: Optional[Dict[str, Any]] = {"query": params.query} if params.query else None
        data = await lusha_get(
            f"/v3/{seg}/prospecting/filters/{params.filter_type}", params=qs
        )
        return _ok(data)
    except Exception as e:
        return _handle_error(e)


class ContactSignalsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    linkedin_url: Optional[str] = Field(
        default=None, description="LinkedIn profile URL of the contact."
    )
    email: Optional[str] = Field(default=None, description="Contact's work email.")
    lusha_person_id: Optional[str] = Field(
        default=None, description="Lusha internal person ID."
    )
    start_date: Optional[str] = Field(
        default=None,
        description="Return signals after this ISO date (YYYY-MM-DD). "
                    "Defaults to last 90 days.",
    )


@mcp.tool(name="lusha_contact_signals")
async def lusha_contact_signals(params: ContactSignalsInput) -> str:
    """Retrieve recent career-change signals for a specific contact:
    job moves, promotions, seniority changes, and company transitions.

    Use these signals to trigger timely outreach when a known contact
    changes roles or companies.
    """
    query: Dict[str, Any] = {"signals": True}
    if params.linkedin_url:
        query["linkedinUrl"] = params.linkedin_url
    if params.email:
        query["email"] = params.email
    if params.lusha_person_id:
        query["personId"] = params.lusha_person_id
    if params.start_date:
        query["signalsStartDate"] = params.start_date

    try:
        data = await lusha_get("/v2/person", params=query)
        signals = data.get("signals", data)
        return _ok(signals)
    except Exception as e:
        return _handle_error(e)


class CompanySignalsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    domains: List[str] = Field(
        ...,
        description=f"Company domains to retrieve signals for (max {MAX_BULK}), e.g. ['stripe.com', 'openai.com'].",
        max_length=MAX_BULK,
    )
    signal_types: Optional[List[str]] = Field(
        default=None,
        description="Filter by signal types: 'hiring', 'funding', 'product_launch', "
                    "'acquisition', 'expansion'. Omit to get all types.",
    )
    start_date: Optional[str] = Field(
        default=None,
        description="Only return signals after this ISO date (YYYY-MM-DD).",
    )


@mcp.tool(name="lusha_company_signals")
async def lusha_company_signals(params: CompanySignalsInput) -> str:
    """Retrieve company-level signals for up to 100 companies: hiring surges,
    funding events, product launches, acquisitions, and expansions.

    Use these signals to prioritize accounts and time your outreach.
    """
    body: Dict[str, Any] = {"domains": params.domains}
    if params.signal_types:
        body["signalTypes"] = params.signal_types
    if params.start_date:
        body["startDate"] = params.start_date

    try:
        data = await lusha_post("/signals/company", body)
        return _ok(data)
    except Exception as e:
        return _handle_error(e)


class ContactLookalikesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    seed_linkedin_urls: Optional[List[str]] = Field(
        default=None,
        description="LinkedIn profile URLs of seed contacts (your best-fit buyers).",
    )
    seed_emails: Optional[List[str]] = Field(
        default=None,
        description="Work emails of seed contacts.",
    )
    limit: int = Field(default=25, ge=1, le=100, description="Max lookalike contacts to return.")
    request_id: Optional[str] = Field(
        default=None,
        description="requestId from a previous call to paginate through more results.",
    )


@mcp.tool(name="lusha_contact_lookalikes")
async def lusha_contact_lookalikes(params: ContactLookalikesInput) -> str:
    """Discover contacts similar to your seed buyers based on role, seniority,
    industry, and company profile.

    Ideal for expanding into new personas similar to those that already convert.
    """
    body: Dict[str, Any] = {"limit": params.limit}
    seeds: List[Dict[str, str]] = []
    for url in (params.seed_linkedin_urls or []):
        seeds.append({"linkedinUrl": url})
    for email in (params.seed_emails or []):
        seeds.append({"email": email})
    if seeds:
        body["seeds"] = seeds
    if params.request_id:
        body["requestId"] = params.request_id

    try:
        data = await lusha_post("/lookalikes/contact", body)
        return _ok(data)
    except Exception as e:
        return _handle_error(e)


class CompanyLookalikesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    seed_domains: List[str] = Field(
        ...,
        description="Domains of your best-performing accounts, e.g. ['stripe.com', 'figma.com'].",
        min_length=1,
    )
    limit: int = Field(default=25, ge=1, le=100)
    request_id: Optional[str] = Field(
        default=None,
        description="requestId from a previous call to paginate through more results.",
    )


@mcp.tool(name="lusha_company_lookalikes")
async def lusha_company_lookalikes(params: CompanyLookalikesInput) -> str:
    """Find companies that look like your best existing customers based on
    firmographics, technographics, size, and industry.

    Use this to expand TAM coverage and ABM lists.
    """
    body: Dict[str, Any] = {
        "seeds": [{"domain": d} for d in params.seed_domains],
        "limit": params.limit,
    }
    if params.request_id:
        body["requestId"] = params.request_id

    try:
        data = await lusha_post("/lookalikes/company", body)
        return _ok(data)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="lusha_account_usage")
async def lusha_account_usage() -> str:
    """Return the current credit usage for your Lusha account:
    total credits, credits used, and credits remaining.

    Rate limit: 5 requests per minute.
    """
    try:
        data = await lusha_get("/account/usage")
        return _ok(data)
    except Exception as e:
        return _handle_error(e)


if __name__ == "__main__":
    transport = "streamable-http" if "--http" in sys.argv else "stdio"
    mcp.run(transport=transport)
