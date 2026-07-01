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
import asyncio
from typing import Awaitable, Callable, Dict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from botbuilder.core import TurnContext, MessageFactory
from botbuilder.integration.aiohttp import (
    CloudAdapter,
    ConfigurationBotFrameworkAuthentication,
)
from botbuilder.schema import Activity, ConversationReference

# (user_text, conversation_id, user_name) -> reply text. Injected by server.py.
AgentReply = Callable[[str, str, str], Awaitable[str]]

# Stored so we can post back proactively after the background run, and (later) push
# unprompted notifications. In-memory for now — a restart forgets a chat until it
# messages the bot again. Persist to Supabase when we add scheduled/manual pushes.
_conv_refs: Dict[str, ConversationReference] = {}
# Hold strong refs to background tasks so the event loop doesn't GC them mid-run.
_bg_tasks: "set[asyncio.Task]" = set()


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


def register_teams_bot(app: FastAPI, agent_reply: AgentReply) -> None:
    """Mount POST /api/messages onto the given FastAPI app."""
    adapter = _build_adapter()
    bot_app_id, *_ = _cfg()

    async def _run_and_post(reference: ConversationReference, text: str, conv_id: str,
                            user_name: str) -> None:
        """Background: run the (slow) agent, then post the result into the chat."""
        try:
            reply = await agent_reply(text, conv_id, user_name)
        except Exception as e:  # noqa: BLE001
            print(f"[TEAMS BOT] agent_reply failed conv={conv_id}: {e}")
            reply = f"Sorry — MASE hit an error handling that: {e}"

        async def _send(tc: TurnContext) -> None:
            await tc.send_activity(MessageFactory.text(reply or "…"))

        try:
            await adapter.continue_conversation(reference, _send, bot_app_id)
            print(f"[TEAMS BOT] proactive reply posted conv={conv_id} ({len(reply or '')} chars)")
        except Exception as e:  # noqa: BLE001
            print(f"[TEAMS BOT] proactive post failed conv={conv_id}: {e}")

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
            text = activity.text or ""
            if activity.entities:  # remove the "@MASE " mention prefix if present
                text = TurnContext.remove_recipient_mention(activity) or text
            text = text.strip()
            if not text:
                return

            # Immediate ack so the activity POST returns well under the Teams timeout.
            await turn.send_activity(MessageFactory.text("On it — MASE is working on this; I'll reply here shortly."))

            # Fire-and-forget the slow work; the proactive post delivers the answer.
            task = asyncio.create_task(
                _run_and_post(_conv_refs[activity.conversation.id], text,
                              activity.conversation.id, user_name)
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
