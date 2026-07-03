"""MASE Teams bot — Bot Framework messaging endpoint.

ONE ABILITY: when the MASE bot is in a chat and a message arrives (or it's @mentioned),
run the existing MASE agent on that text and post the answer back.

ASYNC BY DESIGN. MASE agent runs take 30s–minutes, but the Bot Framework activity POST
must return within ~15s or Teams times out. So we do NOT reply synchronously. Instead:

    message arrives  ->  send an immediate "on it" ack  (returns fast, POST completes)
                     ->  run the agent in a BACKGROUND task
                     ->  when done, PROACTIVELY post the answer back into the same chat
                         via adapter.continue_conversation(stored conversation reference)

This is the push-model half of the Teams integration (the *_mcp_server.py files were the
pull/read half; the earlier delegated-token "Teams MCP" was decommissioned — wrong model).

WIRING (server.py, just before the app is wrapped):
    import teams_bot
    teams_bot.register_teams_bot(_fastapi_app, _teams_agent_reply)

`_teams_agent_reply(user_text, conversation_id) -> str` is injected by server.py so this
module stays decoupled from server internals (agent_manager, recursion limit, etc.).

ENV (this bot is SINGLE-TENANT; App ID 98489e0f-… backs SSO + bot + Outlook — one secret).
The Entra app creds already live in Secrets Manager `mase/app-env` as MS_CLIENT_ID /
MS_CLIENT_SECRET / MS_TENANT_ID, so those are read FIRST — no new secret to provision:
    MS_CLIENT_ID       (fallbacks: BOT_APP_ID, MicrosoftAppId)          — Azure Bot app id
    MS_CLIENT_SECRET   (fallbacks: BOT_APP_PASSWORD, MicrosoftAppPassword) — client secret
    MS_TENANT_ID       (fallbacks: BOT_APP_TENANT_ID, MicrosoftAppTenantId) — tenant
    BOT_APP_TYPE       (fallback: MicrosoftAppType, default "SingleTenant")

LOCAL TEST WITHOUT A SECRET:
    Leave BOT_APP_ID / BOT_APP_PASSWORD blank, run the server, point the Bot Framework
    Emulator at http://localhost:5000/api/messages (Emulator app id/pw blank). The full
    ack -> background -> proactive-reply loop works with no Azure/Teams.
"""

import os
import re
import time
import html
import asyncio
from typing import Awaitable, Callable, Dict

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from botbuilder.core import TurnContext, MessageFactory
from botbuilder.integration.aiohttp import (
    CloudAdapter,
    ConfigurationBotFrameworkAuthentication,
)
from botbuilder.schema import Activity, ConversationReference

try:
    from botbuilder.core.teams import TeamsInfo
except Exception:  # noqa: BLE001
    TeamsInfo = None

# Control-room store (allowlist / activity log / settings). Best-effort: if Supabase
# is unreachable, allowlist checks fail OPEN so a DB blip never locks users out.
try:
    import teams_bot_store as store
except Exception as _e:  # noqa: BLE001
    store = None
    print(f"[TEAMS BOT] store unavailable ({_e}); allowlist + activity log disabled")

# (user_text, conversation_id, user_name, history) -> reply text. Injected by server.py.
AgentReply = Callable[[str, str, str, str], Awaitable[str]]

# Stored so we can post back proactively after the background run, and (later) push
# unprompted notifications. In-memory for now — a restart forgets a chat until it
# messages the bot again. Persist to Supabase when we add scheduled/manual pushes.
_conv_refs: Dict[str, ConversationReference] = {}
# Hold strong refs to background tasks so the event loop doesn't GC them mid-run.
_bg_tasks: "set[asyncio.Task]" = set()


def _mentioned_ids(activity) -> "list[str]":
    """IDs mentioned in the activity. A mention entity's data lives either on a
    typed .mentioned attr or (from raw Teams JSON) in .additional_properties."""
    ids = []
    for e in (TurnContext.get_mentions(activity) or []):
        md = getattr(e, "mentioned", None)
        if md is None:
            md = (getattr(e, "additional_properties", None) or {}).get("mentioned")
        if md is None:
            continue
        mid = md.get("id") if isinstance(md, dict) else getattr(md, "id", None)
        if mid:
            ids.append(mid)
    return ids


# ── Group-chat history reading (Graph, app-only + RSC; behind the history flag) ──────
# Cost control: reading messages via the metered Graph API is billed per message, so we
#   (1) INTENT-GATE — only fetch when the message actually needs chat context,
#   (2) CAP the back-scroll for "first message" queries, and
#   (3) CACHE per conversation with a short TTL so rapid mentions don't re-read/re-bill.
_GRAPH = "https://graph.microsoft.com/v1.0"
HISTORY_MAX = int(os.getenv("TEAMS_HISTORY_MAX", "30"))            # recent-window size
HISTORY_BACKSCROLL_CAP = int(os.getenv("TEAMS_HISTORY_BACKSCROLL_CAP", "500"))  # max msgs for origin
_HIST_TTL_RECENT = float(os.getenv("TEAMS_HISTORY_TTL_S", "300"))   # 5 min
_HIST_TTL_ORIGIN = float(os.getenv("TEAMS_HISTORY_ORIGIN_TTL_S", "3600"))  # first msg is ~immutable
_graph_tok = {"v": "", "exp": 0.0}
_TAG_RE = re.compile(r"<[^>]+>")
_hist_cache: Dict[tuple, tuple] = {}  # (conv_id, mode) -> (text, expiry_epoch)

# Intent detection — decides whether (and how far) to read history.
_ORIGIN_PAT = re.compile(
    r"\b(first (?:message|msg|thing)|very first|when (?:was|did) (?:this|the) (?:group|chat)"
    r"|(?:group|chat) (?:was )?created|start of (?:this|the) (?:group|chat)|who (?:created|started)"
    r"|originally|from the (?:start|beginning)|beginning of (?:this|the))\b", re.I)
_RECENT_PAT = re.compile(
    r"\b(summar|recap|catch (?:me )?up|caught up|brief me|fill me in|so far|earlier|previously"
    r"|what (?:did|was|were|happened|have)|this (?:chat|group|conversation|thread)"
    r"|the (?:conversation|discussion|thread)|discuss|context|going on|missed)\b", re.I)


def _history_intent(text: str) -> "str | None":
    """None → don't read history (saves cost). 'origin' → back-scroll to the group
    start. 'recent' → the last-N window."""
    t = text or ""
    if _ORIGIN_PAT.search(t):
        return "origin"
    if _RECENT_PAT.search(t):
        return "recent"
    return None


def _history_on() -> bool:
    if store is None:
        return False
    try:
        return store.history_enabled()
    except Exception:  # noqa: BLE001
        return False


async def _graph_token() -> str:
    """App-only Graph token (client-credentials on the shared Entra app). Cached."""
    now = time.time()
    if _graph_tok["v"] and now < _graph_tok["exp"] - 60:
        return _graph_tok["v"]
    app_id, app_pw, _t, tenant = _cfg()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={"client_id": app_id, "client_secret": app_pw,
                  "scope": "https://graph.microsoft.com/.default",
                  "grant_type": "client_credentials"},
        )
        r.raise_for_status()
        j = r.json()
    _graph_tok["v"] = j["access_token"]
    _graph_tok["exp"] = now + int(j.get("expires_in", 3600))
    return _graph_tok["v"]


def _strip_html(s: str) -> str:
    # strip tags, then decode HTML entities (&nbsp; &amp; …), collapse whitespace
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", s or ""))).strip()


def _fmt_messages(values) -> "list[str]":
    """Graph message page → 'Name: text' lines (newest-first, as Graph returns).
    Skips system/event rows and empty bodies."""
    out = []
    for m in values:
        if m.get("messageType") != "message":
            continue
        txt = _strip_html((m.get("body") or {}).get("content", ""))
        if not txt:
            continue
        who = (((m.get("from") or {}).get("user") or {}).get("displayName")) or "Unknown"
        out.append(f"{who}: {txt[:500]}")
    return out


async def _graph_get(url: str) -> "httpx.Response":
    tok = await _graph_token()
    async with httpx.AsyncClient(timeout=40) as c:
        return await c.get(url, headers={"Authorization": f"Bearer {tok}"})


async def fetch_history(chat_id: str, limit: int = HISTORY_MAX) -> str:
    """Recent window: the last `limit` messages, chronological. Group chats only
    (19:…@thread.v2 — the conversation id IS the Graph chat id); 1:1 (a:…) skipped."""
    if not chat_id or not chat_id.startswith("19:"):
        return ""
    try:
        r = await _graph_get(f"{_GRAPH}/chats/{chat_id}/messages?$top={max(1, min(limit, 50))}")
        if r.status_code != 200:
            print(f"[TEAMS BOT] history fetch {chat_id[:24]} -> HTTP {r.status_code} {r.text[:150]}")
            return ""
        lines = _fmt_messages(r.json().get("value", []))
        lines.reverse()  # newest-first → chronological
        print(f"[TEAMS BOT] history recent conv={chat_id[:24]} ({len(lines)} msgs)")
        return "\n".join(lines[-limit:])
    except Exception as e:  # noqa: BLE001
        print(f"[TEAMS BOT] history fetch failed: {e}")
        return ""


async def fetch_history_full(chat_id: str, cap: int = HISTORY_BACKSCROLL_CAP) -> "tuple[str, bool]":
    """Back-scroll toward the group's start via @odata.nextLink, up to `cap` messages.
    Returns (chronological text, reached_start). reached_start=True means the first
    line IS the group's original first message; False means we hit the cap first."""
    if not chat_id or not chat_id.startswith("19:"):
        return "", False
    collected: "list[str]" = []
    reached_start = False
    url = f"{_GRAPH}/chats/{chat_id}/messages?$top=50"
    pages = 0
    try:
        while len(collected) < cap and url:
            r = await _graph_get(url)
            if r.status_code != 200:
                print(f"[TEAMS BOT] history full {chat_id[:24]} -> HTTP {r.status_code} {r.text[:120]}")
                break
            j = r.json()
            collected.extend(_fmt_messages(j.get("value", [])))
            url = j.get("@odata.nextLink")
            pages += 1
            if not url:
                reached_start = True
        collected = collected[:cap]
        collected.reverse()  # oldest-first
        print(f"[TEAMS BOT] history full conv={chat_id[:24]} "
              f"({len(collected)} msgs, reached_start={reached_start}, pages={pages})")
        return "\n".join(collected), reached_start
    except Exception as e:  # noqa: BLE001
        print(f"[TEAMS BOT] history full failed: {e}")
        collected.reverse()
        return "\n".join(collected), False


async def get_history(chat_id: str, mode: str) -> str:
    """Cached history for a conversation. mode='recent' (last window) or 'origin'
    (back-scroll to the start). Short TTL so repeat mentions don't re-read/re-bill."""
    now = time.time()
    key = (chat_id, mode)
    hit = _hist_cache.get(key)
    if hit and now < hit[1]:
        return hit[0]
    if mode == "origin":
        text, reached = await fetch_history_full(chat_id)
        if text and not reached:
            text = (f"(Note: showing the earliest ~{HISTORY_BACKSCROLL_CAP} messages the bot "
                    f"could retrieve — the group may have older messages beyond this.)\n{text}")
        ttl = _HIST_TTL_ORIGIN
    else:
        text = await fetch_history(chat_id)
        ttl = _HIST_TTL_RECENT
    _hist_cache[key] = (text, now + ttl)
    return text


def _cfg() -> "tuple[str, str, str, str]":
    # Prefer the MS_* creds already present in mase/app-env (the shared Entra app that
    # backs SSO + bot + Outlook), then bot-specific names, then Bot Framework defaults.
    app_id = (os.getenv("MS_CLIENT_ID") or os.getenv("BOT_APP_ID")
              or os.getenv("MicrosoftAppId", ""))
    app_pw = (os.getenv("MS_CLIENT_SECRET") or os.getenv("BOT_APP_PASSWORD")
              or os.getenv("MicrosoftAppPassword", ""))
    tenant = (os.getenv("MS_TENANT_ID") or os.getenv("BOT_APP_TENANT_ID")
              or os.getenv("MicrosoftAppTenantId", ""))
    app_type = os.getenv("BOT_APP_TYPE") or os.getenv("MicrosoftAppType", "SingleTenant")
    return app_id, app_pw, app_type, tenant


class _BotConfig:
    """botbuilder reads credentials off an OBJECT via getattr with these exact
    attribute names (APP_ID/APP_PASSWORD/APP_TYPE/APP_TENANTID) — NOT a dict and
    NOT the MicrosoftApp* names. Passing a dict makes every hasattr() miss, which
    silently configures a null app id and 401s every Teams token
    ("Invalid AppId passed on token")."""

    def __init__(self, app_id: str, app_pw: str, app_type: str, tenant: str) -> None:
        self.APP_ID = app_id
        self.APP_PASSWORD = app_pw
        self.APP_TYPE = app_type
        self.APP_TENANTID = tenant


def _build_adapter() -> CloudAdapter:
    app_id, app_pw, app_type, tenant = _cfg()
    cfg = _BotConfig(app_id, app_pw, app_type, tenant)
    print(f"[TEAMS BOT] adapter: app_id={'set' if app_id else 'MISSING'} "
          f"type={app_type} tenant={'set' if tenant else 'MISSING'} "
          f"secret={'set' if app_pw else 'MISSING'}")
    return CloudAdapter(ConfigurationBotFrameworkAuthentication(cfg))


async def _resolve_email(turn: TurnContext) -> str:
    """Best-effort sender email/UPN via the Teams roster. The raw activity only carries
    a display name + Teams user id, so we ask TeamsInfo for the member's email."""
    if TeamsInfo is None:
        return ""
    a = turn.activity
    try:
        m = await TeamsInfo.get_member(turn, a.from_property.id)
        return (getattr(m, "email", None) or getattr(m, "user_principal_name", None) or "") or ""
    except Exception as e:  # noqa: BLE001
        print(f"[TEAMS BOT] email resolve failed: {e}")
        return ""


def _is_allowed(email: str, aad_object_id: str) -> bool:
    """Allowlist gate. Fails OPEN (allow) if the store is down or unconfigured, so a DB
    blip never locks users out — enforcement is a deliberate control-room setting."""
    if store is None:
        return True
    try:
        return store.is_allowed(email=email, aad_object_id=aad_object_id)
    except Exception as e:  # noqa: BLE001
        print(f"[TEAMS BOT] allowlist check failed (allowing): {e}")
        return True


def _log(**kw) -> None:
    if store is not None:
        store.log_activity(**kw)


def register_teams_bot(app: FastAPI, agent_reply: AgentReply) -> None:
    """Mount POST /api/messages onto the given FastAPI app."""
    adapter = _build_adapter()
    bot_app_id, *_ = _cfg()

    async def _run_and_post(reference: ConversationReference, text: str, conv_id: str,
                            user_name: str, conv_type: str = None, email: str = "") -> None:
        """Background: run the (slow) agent, then post the result into the chat."""
        status = "ok"
        # Group-chat history context — only when the flag is on, it's a group/channel,
        # AND the message actually needs history (intent-gated to control metered cost).
        history = ""
        if conv_type and conv_type != "personal" and _history_on():
            intent = _history_intent(text)
            if intent:
                history = await get_history(conv_id, intent)
        try:
            reply = await agent_reply(text, conv_id, user_name, history)
        except Exception as e:  # noqa: BLE001
            print(f"[TEAMS BOT] agent_reply failed conv={conv_id}: {e}")
            reply = f"Sorry — MASE hit an error handling that: {e}"
            status = "error"

        async def _send(tc: TurnContext) -> None:
            await tc.send_activity(MessageFactory.text(reply or "…"))

        try:
            await adapter.continue_conversation(reference, _send, bot_app_id)
            print(f"[TEAMS BOT] proactive reply posted conv={conv_id} ({len(reply or '')} chars)")
        except Exception as e:  # noqa: BLE001
            print(f"[TEAMS BOT] proactive post failed conv={conv_id}: {e}")
            status = "error"
        _log(conversation_id=conv_id, conversation_type=conv_type, user_name=user_name,
             user_email=email, direction="out", status=status, text=reply)

    async def on_turn(turn: TurnContext) -> None:
        activity = turn.activity
        # from_property is the sender; .name is their Teams display name. Give it to
        # the agent so it greets the real user instead of hallucinating a name.
        user_name = (activity.from_property.name if activity.from_property else "") or ""
        print(f"[TEAMS BOT] activity type={activity.type} conv={activity.conversation.id} "
              f"user={user_name!r} text={(activity.text or '')[:80]!r}")

        # Remember how to reach this conversation for the proactive follow-up.
        _conv_refs[activity.conversation.id] = TurnContext.get_conversation_reference(activity)

        # Bot added to a chat -> greet. Proves the install + RSC consent path end to end.
        if activity.type == "conversationUpdate" and (activity.members_added or []):
            bot_id = activity.recipient.id if activity.recipient else None
            if any(m.id == bot_id for m in activity.members_added):
                await turn.send_activity(MessageFactory.text(
                    "MASE is here. Mention me or send a message and I'll get back to you."))
            return

        # New message -> THE TRIGGER. Strip any @MASE mention, ack now, run in background.
        if activity.type == "message":
            # In a group chat / channel, Teams delivers EVERY message to the bot. Only
            # respond when MASE is actually @mentioned; in a 1:1 ("personal") chat every
            # message is meant for the bot, so no mention is required.
            conv_type = getattr(activity.conversation, "conversation_type", None)
            if conv_type and conv_type != "personal":
                bot_id = activity.recipient.id if activity.recipient else None
                if bot_id not in _mentioned_ids(activity):
                    return  # group/channel chatter not addressed to MASE — stay quiet

            text = activity.text or ""
            if activity.entities:  # remove the "@MASE " mention prefix if present
                text = TurnContext.remove_recipient_mention(activity) or text
            text = text.strip()
            if not text:
                return

            # Allowlist gate — resolve the sender's email/aad and check the control room.
            aad = getattr(activity.from_property, "aad_object_id", None) if activity.from_property else None
            email = await _resolve_email(turn)
            conv_type_v = getattr(activity.conversation, "conversation_type", None)
            if not _is_allowed(email, aad):
                await turn.send_activity(MessageFactory.text(
                    "You're not on MASE's access list yet. Ask an admin to add you in the MASE control room."))
                _log(conversation_id=activity.conversation.id, conversation_type=conv_type_v,
                     user_name=user_name, user_email=email, direction="in", status="denied", text=text)
                print(f"[TEAMS BOT] denied user={user_name!r} email={email!r}")
                return

            _log(conversation_id=activity.conversation.id, conversation_type=conv_type_v,
                 user_name=user_name, user_email=email, direction="in", status="ok", text=text)

            # Immediate ack so the activity POST returns well under the Teams timeout.
            await turn.send_activity(MessageFactory.text("On it — MASE is working on this; I'll reply here shortly."))

            # Fire-and-forget the slow work; the proactive post delivers the answer.
            task = asyncio.create_task(
                _run_and_post(_conv_refs[activity.conversation.id], text,
                              activity.conversation.id, user_name,
                              conv_type_v, email)
            )
            _bg_tasks.add(task)
            task.add_done_callback(_bg_tasks.discard)

    @app.post("/api/messages")
    async def teams_messages(request: Request):  # noqa: ANN202 (FastAPI route)
        body = await request.json()
        activity = Activity().deserialize(body)
        auth_header = request.headers.get("Authorization", "")
        # CloudAdapter validates the Bot Framework JWT before running on_turn.
        await adapter.process_activity(auth_header, activity, on_turn)
        return JSONResponse({}, status_code=201)

    print("[TEAMS BOT] POST /api/messages registered (async ack -> proactive reply)")
