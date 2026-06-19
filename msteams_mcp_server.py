"""
Microsoft Teams (Microsoft Graph) MCP server  —  READ-FIRST, additive.

Drops into the DeepAgent gateway exactly like the other *_mcp_server.py connectors
(FastMCP over stdio). It does NOT touch any existing connector or MASE code; it only
becomes live when you add an entry to mcp_config.json (see the bottom of this file).

NON-DISRUPTION GUARANTEES
  • READ-ONLY by default. Write/post tools are registered ONLY when
    TEAMS_ENABLE_WRITES=true, so this connector physically cannot post into Teams
    unless you deliberately flip that flag.
  • App-only (client-credentials) auth using the Zycus Graph app registration. It
    reads; it changes nothing on the Teams side.

AUTH (client-credentials / app-only):
  env: TEAMS_TENANT_ID, TEAMS_CLIENT_ID, TEAMS_CLIENT_SECRET
  scope: https://graph.microsoft.com/.default  (uses whatever Application perms are
         granted + admin-consented to the app)

SCOPE REALITY (against the Zycus app's CURRENTLY granted Application permissions):
  • Chats are covered app-only: ChatMessage.Read.All, Chat.ReadWrite.All → the
    teams_list_chats / teams_get_chat / teams_list_chat_messages / teams_list_chat_members
    tools work headless TODAY.
  • Channels are NOT covered app-only yet (only Delegated ChannelMessage.* is granted).
    The teams_* channel tools are included but will return a clear 403 until you add an
    Application/RSC channel scope (ChannelMessage.Read.All, or RSC ChannelMessage.Read.Group).
  Run teams_health first — it decodes the token's `roles` claim and tells you exactly
  which Application permissions the token actually carries.
"""

import os
import json
import time
import base64
import asyncio
from typing import Optional

import httpx

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # some images expose it as the top-level package
    from fastmcp import FastMCP

TENANT_ID = os.environ.get("TEAMS_TENANT_ID", "")
CLIENT_ID = os.environ.get("TEAMS_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("TEAMS_CLIENT_SECRET", "")
WRITES_ENABLED = os.environ.get("TEAMS_ENABLE_WRITES", "false").lower() == "true"
HTTP_TIMEOUT_S = float(os.environ.get("TEAMS_HTTP_TIMEOUT_S", "60"))
MAX_PAGE_FOLLOW = int(os.environ.get("TEAMS_MAX_PAGES", "5"))  # cap nextLink follows

GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"

mcp = FastMCP("msteams-mcp-server")

_token: Optional[str] = None
_token_exp: float = 0.0
_auth_lock: Optional[asyncio.Lock] = None
_sem: Optional[asyncio.Semaphore] = None


def _get_lock() -> asyncio.Lock:
    global _auth_lock
    if _auth_lock is None:
        _auth_lock = asyncio.Lock()
    return _auth_lock


def _get_sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(int(os.environ.get("TEAMS_MAX_CONCURRENCY", "3")))
    return _sem


async def _get_token() -> str:
    """Client-credentials token, cached until ~2 min before expiry."""
    global _token, _token_exp
    if _token and time.time() < _token_exp - 120:
        return _token
    async with _get_lock():
        if _token and time.time() < _token_exp - 120:
            return _token
        if not (TENANT_ID and CLIENT_ID and CLIENT_SECRET):
            raise RuntimeError(
                "Missing TEAMS_TENANT_ID / TEAMS_CLIENT_ID / TEAMS_CLIENT_SECRET env vars."
            )
        data = {
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scope": "https://graph.microsoft.com/.default",
        }
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as c:
            r = await c.post(
                TOKEN_URL,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            r.raise_for_status()
            tok = r.json()
        _token = tok["access_token"]
        _token_exp = time.time() + int(tok.get("expires_in", 3600))
        return _token


def _decode_roles(token: str) -> list:
    """Decode (without verifying) the token's `roles` claim = granted App permissions.
    Diagnostic only — never used for trust decisions."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("roles", [])
    except Exception:
        return []


def _handle_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        s = e.response.status_code
        body = e.response.text[:600]
        if s == 401:
            return f"Error 401 Unauthorized — token/credentials problem. {body}"
        if s == 403:
            return (
                "Error 403 Forbidden — the app token lacks the Application permission "
                "for this endpoint. Channel-message endpoints need ChannelMessage.Read.All "
                "(App) or RSC ChannelMessage.Read.Group; your app may only have these "
                f"Delegated. Run teams_health to see granted roles. {body}"
            )
        if s == 404:
            return f"Error 404 Not Found — check the id (team/channel/chat). {body}"
        if s == 429:
            ra = e.response.headers.get("Retry-After", "?")
            return f"Error 429 Rate limited — retry after {ra}s."
        return f"Error {s}: {body}"
    if isinstance(e, httpx.TimeoutException):
        return "Error: Graph request timed out."
    return f"Error: {type(e).__name__}: {e}"


async def _graph_get(path: str, params: Optional[dict] = None, follow_pages: bool = True) -> dict:
    """GET a Graph path (e.g. '/chats'); collects @odata.nextLink up to MAX_PAGE_FOLLOW."""
    token = await _get_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    url = path if path.startswith("http") else f"{GRAPH}{path}"
    items, pages, first = [], 0, None
    async with _get_sem():
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as c:
            while url and pages <= MAX_PAGE_FOLLOW:
                r = await c.get(url, headers=headers, params=params if pages == 0 else None)
                r.raise_for_status()
                data = r.json()
                if first is None:
                    first = data
                if isinstance(data.get("value"), list):
                    items.extend(data["value"])
                    url = data.get("@odata.nextLink") if follow_pages else None
                    pages += 1
                else:
                    return data  # single object
    return {"value": items, "count": len(items), "pages_followed": pages}


# ───────────────────────── READ TOOLS (always on) ─────────────────────────

@mcp.tool(name="teams_health")
async def teams_health() -> str:
    """Verify auth and show which Application permissions the token actually carries
    (decoded `roles` claim) + a cheap live read. Run this FIRST."""
    try:
        token = await _get_token()
        roles = _decode_roles(token)
        probe = {"chats_read": None}
        try:
            await _graph_get("/chats", params={"$top": 1}, follow_pages=False)
            probe["chats_read"] = "ok"
        except Exception as e:
            probe["chats_read"] = _handle_error(e)
        return json.dumps({
            "auth": "ok",
            "tenant": TENANT_ID,
            "granted_application_roles": sorted(roles),
            "writes_enabled": WRITES_ENABLED,
            "live_probe": probe,
        }, indent=2)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="teams_list_chats")
async def teams_list_chats(top: int = 20) -> str:
    """List chats (1:1 / group / meeting) visible to the app. App perm: Chat.Read.All
    or Chat.ReadWrite.All (granted app-only on the Zycus app)."""
    try:
        return json.dumps(await _graph_get("/chats", params={"$top": top}), indent=2)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="teams_get_chat")
async def teams_get_chat(chat_id: str) -> str:
    """Get one chat's metadata by id."""
    try:
        return json.dumps(await _graph_get(f"/chats/{chat_id}"), indent=2)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="teams_list_chat_messages")
async def teams_list_chat_messages(chat_id: str, top: int = 50) -> str:
    """Read messages in a chat. App perm: ChatMessage.Read.All (granted app-only)."""
    try:
        return json.dumps(
            await _graph_get(f"/chats/{chat_id}/messages", params={"$top": top}), indent=2
        )
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="teams_list_chat_members")
async def teams_list_chat_members(chat_id: str) -> str:
    """List members of a chat. App perm: ChatMember.Read.All / ChatMember.ReadWrite.All."""
    try:
        return json.dumps(await _graph_get(f"/chats/{chat_id}/members"), indent=2)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="teams_list_teams")
async def teams_list_teams(top: int = 50) -> str:
    """List Teams (groups provisioned as a Team). App perm: Group.Read.All / Team.ReadBasic.All
    (NOT granted app-only yet on the Zycus app → may 403 until added)."""
    try:
        params = {
            "$filter": "resourceProvisioningOptions/Any(c:c eq 'Team')",
            "$select": "id,displayName,description,mail",
            "$top": top,
        }
        return json.dumps(await _graph_get("/groups", params=params), indent=2)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="teams_list_channels")
async def teams_list_channels(team_id: str) -> str:
    """List a team's channels. App perm: Channel.ReadBasic.All (NOT granted app-only yet
    → may 403 until added)."""
    try:
        return json.dumps(await _graph_get(f"/teams/{team_id}/channels"), indent=2)
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="teams_list_channel_messages")
async def teams_list_channel_messages(team_id: str, channel_id: str, top: int = 50) -> str:
    """Read a channel's messages. App perm: ChannelMessage.Read.All (a PROTECTED API) or
    RSC ChannelMessage.Read.Group. NOT granted app-only yet → expect 403 until added."""
    try:
        return json.dumps(
            await _graph_get(
                f"/teams/{team_id}/channels/{channel_id}/messages", params={"$top": top}
            ),
            indent=2,
        )
    except Exception as e:
        return _handle_error(e)


@mcp.tool(name="teams_graph_get")
async def teams_graph_get(path: str, odata_query: Optional[str] = None) -> str:
    """Generic READ-ONLY Graph GET escape hatch (e.g. path='/users?$top=5').
    Only GET; cannot write. `path` must start with '/'. `odata_query` is an optional
    raw query string appended (e.g. '$top=10&$select=id,displayName')."""
    try:
        if not path.startswith("/"):
            return "Error: path must start with '/' (a Graph /v1.0 path)."
        if odata_query:
            path = f"{path}{'&' if '?' in path else '?'}{odata_query}"
        return json.dumps(await _graph_get(path), indent=2)
    except Exception as e:
        return _handle_error(e)


# ───────────────── WRITE TOOLS (registered ONLY if TEAMS_ENABLE_WRITES=true) ─────────────────
# NOTE: app-only sending is NOT granted on the Zycus app today (ChannelMessage.Send /
# ChatMessage.Send are Delegated-only). These will 403 app-only until you add RSC
# (ChannelMessage.Send.Group) or use a webhook/bot. They are also OFF unless you opt in.

async def _graph_post(path: str, body: dict) -> dict:
    token = await _get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with _get_sem():
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as c:
            r = await c.post(f"{GRAPH}{path}", headers=headers, json=body)
            r.raise_for_status()
            return r.json()


async def teams_send_channel_message(team_id: str, channel_id: str, html_or_text: str) -> str:
    """[WRITE — opt-in] Post a message to a channel. Requires RSC ChannelMessage.Send.Group
    (app-only) — otherwise 403. Disabled unless TEAMS_ENABLE_WRITES=true."""
    try:
        body = {"body": {"contentType": "html", "content": html_or_text}}
        return json.dumps(
            await _graph_post(f"/teams/{team_id}/channels/{channel_id}/messages", body), indent=2
        )
    except Exception as e:
        return _handle_error(e)


if WRITES_ENABLED:
    mcp.tool(name="teams_send_channel_message")(teams_send_channel_message)


if __name__ == "__main__":
    print(
        f"MS Teams MCP server (read-{'+write' if WRITES_ENABLED else 'only'}) running on stdio",
        flush=True,
    )
    mcp.run()
