import json
import re
import socket
import ipaddress
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from langchain_core.tools import tool

_MAX_CONTENT_LENGTH = 50000
_REMOVE_TAGS = {
    "script", "style", "nav", "footer", "header", "aside",
    "noscript", "iframe", "svg", "form", "button",
}
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_BLOCKED_HOSTNAMES = {"localhost", "127.0.0.1", "0.0.0.0", "[::1]", "metadata.google.internal"}


def _is_public_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return False

    if hostname in _BLOCKED_HOSTNAMES:
        return False

    try:
        addr_infos = socket.getaddrinfo(hostname, None)
        for family, _, _, _, sockaddr in addr_infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
    except (socket.gaierror, ValueError):
        return False

    return True


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(_REMOVE_TAGS):
        tag.decompose()

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    meta_desc = ""
    meta_tag = soup.find("meta", attrs={"name": "description"})
    if meta_tag and meta_tag.get("content"):
        meta_desc = meta_tag["content"].strip()

    body = soup.find("body") or soup
    lines = []
    for element in body.stripped_strings:
        line = element.strip()
        if line:
            lines.append(line)

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)

    if len(text) > _MAX_CONTENT_LENGTH:
        text = text[:_MAX_CONTENT_LENGTH] + f"\n\n...[TRUNCATED at {_MAX_CONTENT_LENGTH} chars]..."

    return title, meta_desc, text


@tool
def web_scrape(url: str) -> str:
    """Fetch and extract the text content from a public web page URL.

    Use this to scrape a website and get its readable content. Works best with
    URLs obtained from web_search_with_urls. Strips out navigation, ads, scripts,
    and other non-content elements, returning clean text.

    Only works with public URLs. Cannot access localhost, internal networks, or
    private IP addresses.

    Args:
        url: The full URL to scrape (must start with http:// or https://).

    Returns:
        JSON string with the page title, description, URL, and extracted text content.
    """
    if not url.startswith(("http://", "https://")):
        return json.dumps({
            "status": "error",
            "error": "URL must start with http:// or https://",
            "url": url,
        })

    if not _is_public_url(url):
        return json.dumps({
            "status": "error",
            "error": "Cannot access internal, private, or localhost URLs. Only public websites are allowed.",
            "url": url,
        })

    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            resp = client.get(url, headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            })

        if resp.status_code >= 400:
            return json.dumps({
                "status": "error",
                "error": f"HTTP {resp.status_code}",
                "url": str(resp.url),
            })

        content_type = resp.headers.get("content-type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            if "application/json" in content_type:
                try:
                    data = resp.json()
                    return json.dumps({
                        "status": "success",
                        "url": str(resp.url),
                        "content_type": "json",
                        "content": data if len(json.dumps(data)) < _MAX_CONTENT_LENGTH else str(data)[:_MAX_CONTENT_LENGTH],
                    }, indent=2, default=str)
                except Exception:
                    pass
            if "text/" in content_type:
                text = resp.text[:_MAX_CONTENT_LENGTH]
                return json.dumps({
                    "status": "success",
                    "url": str(resp.url),
                    "content_type": content_type.split(";")[0],
                    "content": text,
                }, indent=2)
            return json.dumps({
                "status": "error",
                "error": f"Unsupported content type: {content_type.split(';')[0]}",
                "url": str(resp.url),
            })

        title, meta_desc, text = _html_to_text(resp.text)

        return json.dumps({
            "status": "success",
            "url": str(resp.url),
            "title": title,
            "description": meta_desc,
            "content_length": len(text),
            "content": text,
        }, indent=2)

    except httpx.TimeoutException:
        return json.dumps({
            "status": "error",
            "error": "Request timed out after 30 seconds",
            "url": url,
        })
    except Exception as e:
        return json.dumps({
            "status": "error",
            "error": str(e),
            "url": url,
        })
