"""Microsoft Teams MCP server (Microsoft Graph v1.0).

Read/write access to Teams: list joined teams, channels, chats; read and send
channel + chat messages; reply to threads; look up users.

AUTH MODEL — delegated OAuth (NOT app-only)
-------------------------------------------
Microsoft Graph does NOT permit sending channel/chat messages with app-only
(client_credentials) tokens — POST .../messages requires a delegated (signed-in
user) context. We therefore authenticate with a long-lived refresh token, the
same shape as linkedin_mcp_server.py's stored access token but auto-refreshed.

This is a PUBLIC client (Authentication -> "Allow public client flows" = Yes,
nativeclient redirect under "Mobile and desktop applications"). Public clients
must NOT send a client_secret, so none is configured here.

One-time setup (see docs/integrations.md "Microsoft Teams"):
  1. Register an app in Entra ID (Azure AD), single tenant.
  2. Add redirect URI https://login.microsoftonline.com/common/oauth2/nativeclient
     under "Mobile and desktop applications"; set "Allow public client flows" = Yes.
  3. Grant DELEGATED Microsoft Graph permissions + admin consent:
       offline_access, User.Read, Team.ReadBasic.All, Channel.ReadBasic.All,
       ChannelMessage.Read.All, ChannelMessage.Send,
       Chat.Read, Chat.ReadWrite, ChatMessage.Send, User.ReadBasic.All
  4. Run the auth-code flow once as the service user, capture the refresh_token.
  5. Set secrets: TEAMS_TENANT_ID, TEAMS_CLIENT_ID, TEAMS_REFRESH_TOKEN.

Required env (declared in mcp_config.json under mcp_servers.teams.env —
MultiServerMCPClient does NOT propagate parent env):
  TEAMS_TENANT_ID, TEAMS_CLIENT_ID, TEAMS_REFRESH_TOKEN
"""

import os
import json
import time
import threading
from typing import Optional, Any

import httpx
from fastmcp import FastMCP

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TIMEOUT = 30.0

TENANT_ID = os.environ.get("TEAMS_TENANT_ID", "").strip()
CLIENT_ID = os.environ.get("TEAMS_CLIENT_ID", "").strip()
REFRESH_TOKEN = os.environ.get("TEAMS_REFRESH_TOKEN", "").strip()

# Delegated scopes requested on every refresh. offline_access keeps the
# refresh token rolling. Keep in sync with the admin-consented permission set.
_SCOPES = (
    "offline_access User.Read Team.ReadBasic.All Channel.ReadBasic.All "
    "ChannelMessage.Read.All ChannelMessage.Send "
    "Chat.Read Chat.ReadWrite ChatMessage.Send User.ReadBasic.All"
)

_token_lock = threading.Lock()
_access_token: Optional[str] = None
_token_expiry: float = 0.0
# A refresh-token rotation may hand back a new refresh token; hold the latest.
_current_refresh_token: str = REFRESH_TOKEN

mcp = FastMCP(
    name="Microsoft Teams",
    instructions=(
        "Use this server to read and send Microsoft Teams messages via Microsoft "
        "Graph v1.0. You can list the joined teams, a team's channels, and 1:1 / "
        "group chats; read channel and chat message history; send new channel "
        "messages, reply to a channel thread, and send chat messages; and look up "
        "users by name or email. "
        "IDs flow top-down: teams_list_joined_teams -> team_id; "
        "teams_list_channels(team_id) -> channel_id; then read/send on that channel. "
        "Message bodies accept plain text by default or HTML when content_type='html'. "
        "Auth is delegated (acts AS the configured service user) — messages are sent "
        "from that user's identity, and chat/channel visibility is limited to what "
        "that user is a member of."
    ),
)


def _token_url() -> str:
    if not TENANT_ID:
        raise RuntimeError(
            "TEAMS_TENANT_ID is not set. Configure TEAMS_TENANT_ID, TEAMS_CLIENT_ID, "
            "TEAMS_CLIENT_SECRET and TEAMS_REFRESH_TOKEN (see docs/integrations.md)."
        )
    return f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"


def _get_access_token() -> str:
    """Return a cached access token, refreshing via the stored refresh token."""
    global _access_token, _token_expiry, _current_refresh_token

    with _token_lock:
        if _access_token and time.time() < _token_expiry:
            return _access_token

        if not (CLIENT_ID and _current_refresh_token):
            raise RuntimeError(
                "Microsoft Teams auth is not configured. Required env: "
                "TEAMS_TENANT_ID, TEAMS_CLIENT_ID, TEAMS_REFRESH_TOKEN. "
                "Obtain the refresh token via the one-time delegated auth-code "
                "flow (see docs/integrations.md 'Microsoft Teams')."
            )

        # Public client (Allow public client flows = Yes): no client_secret.
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(
                _token_url(),
                data={
                    "grant_type": "refresh_token",
                    "client_id": CLIENT_ID,
                    "refresh_token": _current_refresh_token,
                    "scope": _SCOPES,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Microsoft Graph token refresh failed ({resp.status_code}): {resp.text}. "
                "The refresh token may be expired/revoked — re-run the delegated "
                "auth-code flow and update TEAMS_REFRESH_TOKEN."
            )
        data = resp.json()
        _access_token = data["access_token"]
        _token_expiry = time.time() + int(data.get("expires_in", 3600)) - 60
        # Azure AD rotates refresh tokens; keep the newest for the process lifetime.
        if data.get("refresh_token"):
            _current_refresh_token = data["refresh_token"]
        return _access_token


def _headers(content_type: str = "application/json") -> dict:
    return {
        "Authorization": f"Bearer {_get_access_token()}",
        "Content-Type": content_type,
    }


def _request(method: str, path_or_url: str, *, params: Optional[dict] = None,
             body: Optional[dict] = None) -> Any:
    url = path_or_url if path_or_url.startswith("http") else f"{GRAPH_BASE}{path_or_url}"
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.request(
            method, url, headers=_headers(), params=params, json=body,
        )
    if resp.status_code == 429:
        # Honour Retry-After once, then re-raise on a second failure.
        retry_after = float(resp.headers.get("Retry-After", "2"))
        time.sleep(min(retry_after, 10))
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.request(
                method, url, headers=_headers(), params=params, json=body,
            )
    if resp.status_code >= 400:
        raise RuntimeError(f"Graph API error {resp.status_code} on {method} {url}: {resp.text}")
    if resp.status_code == 204 or not resp.content:
        return {}
    try:
        return resp.json()
    except Exception:
        return {"raw_response": resp.text}


def _get(path: str, params: Optional[dict] = None) -> Any:
    return _request("GET", path, params=params)


def _paginate(path: str, params: Optional[dict] = None, limit: int = 200) -> list:
    """Follow @odata.nextLink until `limit` items are collected."""
    items: list = []
    page = _get(path, params=params)
    while True:
        items.extend(page.get("value", []))
        next_link = page.get("@odata.nextLink")
        if not next_link or len(items) >= limit:
            break
        page = _request("GET", next_link)
    return items[:limit]


def _post(path: str, body: dict) -> Any:
    return _request("POST", path, body=body)


def _message_body(text: str, content_type: str) -> dict:
    ct = "html" if str(content_type).lower() == "html" else "text"
    return {"body": {"contentType": ct, "content": text}}


def _slim_message(m: dict) -> dict:
    """Reduce a Graph chatMessage to the fields that matter for reading."""
    frm = (m.get("from") or {}).get("user") or {}
    body = m.get("body") or {}
    return {
        "id": m.get("id"),
        "created": m.get("createdDateTime"),
        "from": frm.get("displayName"),
        "from_id": frm.get("id"),
        "content_type": body.get("contentType"),
        "content": body.get("content"),
        "reply_to_id": m.get("replyToId"),
        "importance": m.get("importance"),
        "web_url": m.get("webUrl"),
    }


# ---------------------------------------------------------------------------
# Teams & channels
# ---------------------------------------------------------------------------

@mcp.tool()
def teams_list_joined_teams() -> str:
    """List all Microsoft Teams the authenticated service user is a member of.

    Returns:
        JSON with each team's id, displayName, and description. Use the id as
        team_id for teams_list_channels and the channel message tools.
    """
    teams = _paginate("/me/joinedTeams", limit=200)
    slim = [{"team_id": t.get("id"), "name": t.get("displayName"),
             "description": t.get("description")} for t in teams]
    return json.dumps({"status": "success", "total": len(slim), "teams": slim},
                      indent=2, default=str)


@mcp.tool()
def teams_list_channels(team_id: str) -> str:
    """List the channels in a Microsoft Team.

    Args:
        team_id: The team id from teams_list_joined_teams.

    Returns:
        JSON with each channel's id, displayName, description and
        membershipType (standard/private/shared). Use channel_id with the
        channel message tools.
    """
    channels = _paginate(f"/teams/{team_id}/channels", limit=200)
    slim = [{"channel_id": c.get("id"), "name": c.get("displayName"),
             "description": c.get("description"),
             "membership": c.get("membershipType"),
             "web_url": c.get("webUrl")} for c in channels]
    return json.dumps({"status": "success", "team_id": team_id,
                       "total": len(slim), "channels": slim},
                      indent=2, default=str)


@mcp.tool()
def teams_list_channel_messages(team_id: str, channel_id: str, limit: int = 30) -> str:
    """Read recent top-level messages in a Teams channel (newest first).

    Note: replies are nested under each message and are not expanded here; use
    teams_list_message_replies for a specific thread.

    Args:
        team_id: Team id from teams_list_joined_teams.
        channel_id: Channel id from teams_list_channels.
        limit: Max messages to return (default 30, max 200).

    Returns:
        JSON with slimmed messages: id, created, from, content, reply_to_id.
    """
    limit = max(1, min(int(limit), 200))
    msgs = _paginate(f"/teams/{team_id}/channels/{channel_id}/messages",
                     params={"$top": min(limit, 50)}, limit=limit)
    return json.dumps({"status": "success", "team_id": team_id,
                       "channel_id": channel_id, "total": len(msgs),
                       "messages": [_slim_message(m) for m in msgs]},
                      indent=2, default=str)


@mcp.tool()
def teams_list_message_replies(team_id: str, channel_id: str, message_id: str,
                               limit: int = 50) -> str:
    """Read the replies in a Teams channel message thread (newest first).

    Args:
        team_id: Team id.
        channel_id: Channel id.
        message_id: The parent message id (from teams_list_channel_messages).
        limit: Max replies to return (default 50, max 200).

    Returns:
        JSON with slimmed reply messages.
    """
    limit = max(1, min(int(limit), 200))
    replies = _paginate(
        f"/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies",
        params={"$top": min(limit, 50)}, limit=limit)
    return json.dumps({"status": "success", "message_id": message_id,
                       "total": len(replies),
                       "replies": [_slim_message(m) for m in replies]},
                      indent=2, default=str)


@mcp.tool()
def teams_send_channel_message(team_id: str, channel_id: str, message: str,
                               content_type: str = "text") -> str:
    """Post a new top-level message to a Teams channel (as the service user).

    Args:
        team_id: Team id from teams_list_joined_teams.
        channel_id: Channel id from teams_list_channels.
        message: The message body. Plain text by default; pass HTML markup and
            set content_type="html" for formatting/mentions.
        content_type: "text" (default) or "html".

    Returns:
        JSON with the created message's id, web_url and timestamp.
    """
    created = _post(f"/teams/{team_id}/channels/{channel_id}/messages",
                    _message_body(message, content_type))
    return json.dumps({"status": "success", "message_id": created.get("id"),
                       "created": created.get("createdDateTime"),
                       "web_url": created.get("webUrl")}, indent=2, default=str)


@mcp.tool()
def teams_reply_to_channel_message(team_id: str, channel_id: str, message_id: str,
                                   message: str, content_type: str = "text") -> str:
    """Reply within an existing Teams channel message thread.

    Args:
        team_id: Team id.
        channel_id: Channel id.
        message_id: The parent message id to reply under.
        message: The reply body (text by default, HTML if content_type="html").
        content_type: "text" (default) or "html".

    Returns:
        JSON with the created reply's id and timestamp.
    """
    created = _post(
        f"/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies",
        _message_body(message, content_type))
    return json.dumps({"status": "success", "reply_id": created.get("id"),
                       "created": created.get("createdDateTime")},
                      indent=2, default=str)


# ---------------------------------------------------------------------------
# Chats (1:1 and group)
# ---------------------------------------------------------------------------

@mcp.tool()
def teams_list_chats(limit: int = 50) -> str:
    """List the authenticated user's 1:1 and group chats (most recent first).

    Args:
        limit: Max chats to return (default 50, max 200).

    Returns:
        JSON with each chat's id, chatType (oneOnOne/group/meeting), topic and
        last-updated time. Use chat_id with the chat message tools.
    """
    limit = max(1, min(int(limit), 200))
    chats = _paginate("/me/chats", params={"$top": min(limit, 50)}, limit=limit)
    slim = [{"chat_id": c.get("id"), "type": c.get("chatType"),
             "topic": c.get("topic"), "updated": c.get("lastUpdatedDateTime"),
             "web_url": c.get("webUrl")} for c in chats]
    return json.dumps({"status": "success", "total": len(slim), "chats": slim},
                      indent=2, default=str)


@mcp.tool()
def teams_list_chat_messages(chat_id: str, limit: int = 30) -> str:
    """Read recent messages from a 1:1 or group chat (newest first).

    Args:
        chat_id: Chat id from teams_list_chats.
        limit: Max messages to return (default 30, max 200).

    Returns:
        JSON with slimmed messages: id, created, from, content.
    """
    limit = max(1, min(int(limit), 200))
    msgs = _paginate(f"/me/chats/{chat_id}/messages",
                     params={"$top": min(limit, 50)}, limit=limit)
    return json.dumps({"status": "success", "chat_id": chat_id, "total": len(msgs),
                       "messages": [_slim_message(m) for m in msgs]},
                      indent=2, default=str)


@mcp.tool()
def teams_send_chat_message(chat_id: str, message: str,
                            content_type: str = "text") -> str:
    """Send a message to an existing 1:1 or group chat (as the service user).

    Args:
        chat_id: Chat id from teams_list_chats.
        message: The message body (text by default, HTML if content_type="html").
        content_type: "text" (default) or "html".

    Returns:
        JSON with the created message's id and timestamp.
    """
    created = _post(f"/me/chats/{chat_id}/messages",
                    _message_body(message, content_type))
    return json.dumps({"status": "success", "message_id": created.get("id"),
                       "created": created.get("createdDateTime")},
                      indent=2, default=str)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@mcp.tool()
def teams_search_users(query: str, limit: int = 15) -> str:
    """Find users in the directory by name or email (for resolving recipients).

    Args:
        query: Display-name fragment or email/UPN to search for.
        limit: Max users to return (default 15, max 50).

    Returns:
        JSON with each match's id, displayName, mail and userPrincipalName.
        The id / userPrincipalName can be used to start or address chats.
    """
    limit = max(1, min(int(limit), 50))
    q = query.replace("'", "''")
    params = {
        "$filter": (
            f"startswith(displayName,'{q}') or startswith(mail,'{q}') "
            f"or startswith(userPrincipalName,'{q}')"
        ),
        "$select": "id,displayName,mail,userPrincipalName,jobTitle",
        "$top": limit,
    }
    users = _paginate("/users", params=params, limit=limit)
    slim = [{"id": u.get("id"), "name": u.get("displayName"),
             "mail": u.get("mail"), "upn": u.get("userPrincipalName"),
             "title": u.get("jobTitle")} for u in users]
    return json.dumps({"status": "success", "total": len(slim), "users": slim},
                      indent=2, default=str)


if __name__ == "__main__":
    mcp.run(transport="stdio")
