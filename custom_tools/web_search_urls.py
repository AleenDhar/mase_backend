import json
from typing import Optional

from langchain_community.utilities import DuckDuckGoSearchAPIWrapper
from langchain_core.tools import tool

_search_wrapper = DuckDuckGoSearchAPIWrapper(max_results=10)


@tool
def web_search_with_urls(query: str, max_results: int = 10) -> str:
    """Search the web and return results WITH URLs. Each result includes a title, URL/link, and snippet.

    Use this when you need the actual website URLs from search results (e.g., to scrape them
    with web_scrape, to share links with the user, or to visit specific pages).

    Args:
        query: The search query string.
        max_results: Maximum number of results to return (default 10, max 20).

    Returns:
        JSON string with a list of search results, each containing title, link, and snippet.
    """
    try:
        max_results = min(max(1, max_results), 20)
        results = _search_wrapper.results(query, max_results=max_results)

        formatted = []
        for r in results:
            formatted.append({
                "title": r.get("title", ""),
                "url": r.get("link", ""),
                "snippet": r.get("snippet", ""),
            })

        return json.dumps({
            "status": "success",
            "query": query,
            "total_results": len(formatted),
            "results": formatted,
        }, indent=2)
    except Exception as e:
        error_msg = str(e)
        if "429" in error_msg or "Too Many Requests" in error_msg:
            return json.dumps({
                "status": "error",
                "error": "Rate limited. Try again in a moment.",
                "query": query,
            })
        return json.dumps({
            "status": "error",
            "error": error_msg,
            "query": query,
        })
