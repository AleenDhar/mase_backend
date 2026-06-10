import json
import os
from typing import Optional
import httpx
from pydantic import BaseModel, Field, ConfigDict
from mcp.server.fastmcp import FastMCP

BASE_URL = "https://ext.gojiberry.ai"
API_KEY = os.environ.get("GOJIBERRY_API_KEY", "")

mcp = FastMCP("gojiberry-mcp-server")


def _get_headers() -> dict:
    key = API_KEY
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


async def _api_get(path: str, params: dict = None) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{BASE_URL}{path}",
            headers=_get_headers(),
            params=params or {},
        )
        response.raise_for_status()
        return response.json()


def _handle_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status == 401:
            return "Error: Unauthorized. Check your GOJIBERRY_API_KEY environment variable."
        if status == 404:
            return "Error: Resource not found. Verify the ID exists."
        if status == 429:
            return "Error: Rate limit exceeded (100 req/min). Please wait before retrying."
        return f"Error: API request failed with status {status}: {e.response.text}"
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request timed out. Please try again."
    return f"Error: {type(e).__name__}: {str(e)}"


@mcp.tool(name="gojiberry_get_all_contacts")
async def gojiberry_get_all_contacts(
    search: Optional[str] = None,
    agent: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Retrieve all contacts from Gojiberry with optional filtering and pagination.
    Supports filtering by search term, agent, and date range.
    """
    try:
        query: dict = {}
        if search:
            query["search"] = search
        if agent is not None:
            query["agent"] = agent
        if date_from:
            query["dateFrom"] = date_from
        if date_to:
            query["dateTo"] = date_to
        query["limit"] = limit
        query["offset"] = offset

        data = await _api_get("/v1/contact", params=query)

        contacts = data if isinstance(data, list) else data.get("contacts", data.get("data", []))
        total = len(contacts) if isinstance(data, list) else data.get("total", len(contacts))

        result = {
            "total": total,
            "count": len(contacts),
            "offset": offset,
            "contacts": contacts,
            "has_more": total > offset + len(contacts),
            "next_offset": offset + len(contacts) if total > offset + len(contacts) else None,
        }
        return json.dumps(result, indent=2)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="gojiberry_get_contact")
async def gojiberry_get_contact(contact_id: int) -> str:
    """Retrieve a single contact by their unique ID."""
    try:
        data = await _api_get(f"/v1/contact/{contact_id}")
        return json.dumps(data, indent=2)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="gojiberry_get_all_lists")
async def gojiberry_get_all_lists() -> str:
    """Retrieve all lists from Gojiberry with their contact counts and campaign info."""
    try:
        data = await _api_get("/v1/list")
        lists = data if isinstance(data, list) else data.get("lists", data.get("data", []))
        result = {
            "count": len(lists),
            "lists": lists,
        }
        return json.dumps(result, indent=2)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="gojiberry_get_list")
async def gojiberry_get_list(list_id: int) -> str:
    """Retrieve a single list by ID, including its contacts."""
    try:
        data = await _api_get(f"/v1/list/{list_id}")
        return json.dumps(data, indent=2)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="gojiberry_get_all_campaigns")
async def gojiberry_get_all_campaigns() -> str:
    """Retrieve all campaigns from Gojiberry with steps, tone, goal, and LinkedIn seat info."""
    try:
        data = await _api_get("/v1/campaign")
        campaigns = data if isinstance(data, list) else data.get("campaigns", data.get("data", []))
        result = {
            "count": len(campaigns),
            "campaigns": campaigns,
        }
        return json.dumps(result, indent=2)
    except Exception as e:
        return _handle_error(e)


if __name__ == "__main__":
    print("Gojiberry MCP server running on stdio")
    mcp.run()
