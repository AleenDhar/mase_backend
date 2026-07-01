# MASE Teams Bot — Setup Runbook

Wire the **MASE Teams bot** (Bot Framework) into the FastAPI backend so it can be added
to a chat, read past history, trigger on every new message, be manually @mentioned, and
post replies.

> **Model note.** The two files in this repo — `msteams_mcp_server.py` (app-only Graph)
> and `teams_mcp_server.py` (delegated Graph) — are **read connectors** (pull model). They
> do NOT receive message events. The bot's "trigger on new message / manual trigger /
> write" behaviour comes from the **Bot Framework messaging endpoint** you build in
> step 3. The Graph connector is reused only to read *history that predates* the install.

---

## 0. Architecture at a glance

```
Teams chat  ──(every message / @mention / install event)──►  POST https://<host>/api/messages
                                                                     │  (Bot Framework JWT)
                                                                     ▼
                                              server.py  /api/messages  (NEW)
                                                 │  build message list from Activity
                                                 │  read prior history via Graph RSC (msteams connector)
                                                 ▼
                                          existing MASE agent  (agent_manager.get_agent(), see server.py:3261)
                                                 │  reply text
                                                 ▼
                                   Bot Connector API  ──►  message appears in the Teams chat
```

Two ways the bot sends:
- **Reply** — inside a turn, `turn_context.send_activity(...)`.
- **Proactive / manual** — replay a stored **conversation reference** (step 6) from a
  separate backend route, so MASE can push a deal update into a chat with no user prompt.

---

## 1. Prerequisites (confirm before coding)

- [x] **Azure Bot resource** exists with **Microsoft App ID + client secret**. *(You have this.)*
- [ ] App ID is the **same** value in all three places: Teams app manifest
      `webApplicationInfo.id`, the Azure Bot resource, and the backend env `MicrosoftAppId`.
      A mismatch breaks JWT validation and RSC.
- [ ] A **public HTTPS host** for the backend (the AWS deploy — `buildspec.yml` / `Dockerfile`,
      port 5000). The bot messaging endpoint must be reachable at `https://<host>/api/messages`.
- [ ] Single-tenant vs multi-tenant: note which your Azure Bot uses → sets `MicrosoftAppType`
      and `MicrosoftAppTenantId` below.

---

## 2. Verify the Teams app manifest

The permissions you granted are **RSC (Resource-Specific Consent)** — declared in the app
manifest, consented once when the app is installed into a chat/team. Confirm the manifest
(Developer Portal → your MASE app → *Edit manifest*, or `manifest.json`) contains:

```jsonc
{
  "bots": [
    {
      "botId": "<MICROSOFT_APP_ID>",
      "scopes": ["personal", "team", "groupchat"],
      "supportsFiles": false,
      "isNotificationOnly": false
    }
  ],
  "webApplicationInfo": {
    "id": "<MICROSOFT_APP_ID>",
    "resource": "https://RscBasedStoreApp"   // any URI; RSC does not use a static resource scope
  },
  "authorization": {
    "permissions": {
      "resourceSpecific": [
        { "name": "ChatMessage.Read.Chat",     "type": "Application" },  // read this chat's messages
        { "name": "ChatMember.Read.Chat",       "type": "Application" },  // read chat members
        { "name": "ChannelMessage.Read.Group",  "type": "Application" },  // read messages in this team
        { "name": "TeamMember.Read.Group",       "type": "Application" }   // read team members
      ]
    }
  }
}
```

Those four map exactly to the permission screen you saw ("Read this chat's messages",
"Read this chat's members", "Read messages in this team", "Read this team's members").
Send/receive of messages is a **bot capability**, not an RSC scope — it comes from the
`bots` block, not `resourceSpecific`.

> **Channel trigger caveat.** With `ChannelMessage.Read.Group` the bot receives **all**
> channel messages. Without it, a bot in a *channel* only gets messages where it is
> **@mentioned**. In a **group chat** the bot receives all messages once installed either way.

---

## 3. Backend: dependencies + env

Add to `requirements.txt`:

```
botbuilder-core
botbuilder-integration-aiohttp   # provides CloudAdapter + JWT auth; usable from FastAPI
```

Set these secrets (same place the other connector secrets are injected — see `deploy.ps1`
/ task-def env, not committed):

```
MicrosoftAppId=<MICROSOFT_APP_ID>
MicrosoftAppPassword=<CLIENT_SECRET>
MicrosoftAppType=SingleTenant          # or MultiTenant
MicrosoftAppTenantId=<TENANT_ID>       # required for SingleTenant

# For reading pre-install history via the Graph connector (app-only token):
TEAMS_TENANT_ID=<TENANT_ID>
TEAMS_CLIENT_ID=<MICROSOFT_APP_ID>     # reuse the bot's app registration
TEAMS_CLIENT_SECRET=<CLIENT_SECRET>
```

> The history read reuses `msteams_mcp_server.py`'s `_get_token()` + `_graph_get()`
> (app-only). RSC grants the app permission scoped to the specific chat/team the bot was
> added to, so `GET /chats/{chatId}/messages` succeeds app-only — no tenant-wide admin
> consent needed.

---

## 4. Backend: add the `POST /api/messages` endpoint

New handler in `server.py` (place near the other `@app.post` routes). This scaffolds the
adapter, routes activities, and calls the **existing MASE agent path** the same way
`/api/chat` does at `server.py:3261`.

```python
# ── Teams Bot (Bot Framework) ──────────────────────────────────────────────
from botbuilder.core import TurnContext, MessageFactory
from botbuilder.core.integration import aiohttp_error_middleware  # noqa: F401 (optional)
from botbuilder.integration.aiohttp import CloudAdapter, ConfigurationBotFrameworkAuthentication
from botbuilder.schema import Activity, ConversationReference

_bot_auth = ConfigurationBotFrameworkAuthentication(
    {
        "MicrosoftAppId": os.getenv("MicrosoftAppId", ""),
        "MicrosoftAppPassword": os.getenv("MicrosoftAppPassword", ""),
        "MicrosoftAppType": os.getenv("MicrosoftAppType", "SingleTenant"),
        "MicrosoftAppTenantId": os.getenv("MicrosoftAppTenantId", ""),
    }
)
_bot_adapter = CloudAdapter(_bot_auth)

# conversation references keyed by conversation id — enables proactive sends (step 6)
_teams_conv_refs: dict[str, ConversationReference] = {}


async def _mase_reply_for_text(user_text: str, chat_id: str) -> str:
    """Run the existing MASE agent on one user turn and return its text.
    Mirrors the /api/chat path (server.py:3261): get the agent, hand it the
    message, collect the final text. Keep history reads as an agent tool call
    (the msteams Graph connector) rather than stuffing full history here."""
    agent = await agent_manager.get_agent()
    result = await agent.ainvoke({"messages": [{"role": "user", "content": user_text}]})
    # adapt to however /api/chat extracts the final assistant message:
    msgs = result.get("messages", []) if isinstance(result, dict) else []
    return (msgs[-1].content if msgs else "") or "…"


async def _on_turn(turn: TurnContext):
    activity = turn.activity

    # stash a conversation reference for later proactive/manual sends
    ref = TurnContext.get_conversation_reference(activity)
    _teams_conv_refs[activity.conversation.id] = ref

    if activity.type == "conversationUpdate" and activity.members_added:
        bot_id = activity.recipient.id
        if any(m.id == bot_id for m in activity.members_added):
            # bot was just added → optionally pull prior history and greet
            await turn.send_activity(MessageFactory.text(
                "MASE is here. Ask me about a deal, or say 'summarize this chat'."))
        return

    if activity.type == "message":
        text = (activity.text or "").strip()
        # In a channel the bot only gets @mentioned messages; strip the mention:
        text = TurnContext.remove_recipient_mention(activity) or text
        reply = await _mase_reply_for_text(text, activity.conversation.id)
        await turn.send_activity(MessageFactory.text(reply))


@app.post("/api/messages")
async def teams_messages(request: Request):
    body = await request.json()
    activity = Activity().deserialize(body)
    auth_header = request.headers.get("Authorization", "")
    await _bot_adapter.process_activity(auth_header, activity, _on_turn)
    return JSONResponse({}, status_code=200)
```

Notes:
- `agent.ainvoke(...)` above is a **placeholder** — match it to how `/api/chat` actually
  drives the agent (streaming vs `ainvoke`, message wrapping via `_build_message_content`,
  the summarize/trim logic at `server.py:3263–3271`). Reuse that code path; don't fork it.
- Keep the run bounded by the same concurrency backstop `/api/chat` uses
  (`_reserve_run_slot` / `MAX_CONCURRENT_SESSIONS`) so Teams traffic can't exhaust sessions.

---

## 5. Reading history that predates the install

When MASE needs the back-scroll (e.g. "summarize this chat"), have the agent call the
existing Graph connector rather than reimplementing it:

```python
import msteams_mcp_server as _teams_graph

async def read_chat_history(chat_id: str, top: int = 50) -> dict:
    # app-only token + Graph GET, already implemented in the connector
    return await _teams_graph._graph_get(f"/chats/{chat_id}/messages", {"$top": top})
```

`activity.conversation.id` from the incoming message **is** the Graph `chatId` for group
chats, so it plugs straight in. This is where your "read past history" requirement is met.

---

## 6. Manual / proactive sends (deal pushes, scheduled nudges)

Add a route that replays a stored conversation reference — lets MASE post into a chat with
no user message (e.g. a nightly pipeline alert):

```python
@app.post("/api/teams/notify")
async def teams_notify(request: Request):
    body = await request.json()
    conv_id, text = body["conversation_id"], body["text"]
    ref = _teams_conv_refs.get(conv_id)
    if not ref:
        raise HTTPException(404, "no conversation reference for that chat yet")
    async def _send(turn: TurnContext):
        await turn.send_activity(MessageFactory.text(text))
    await _bot_adapter.continue_conversation(ref, _send, os.getenv("MicrosoftAppId"))
    return {"ok": True}
```

> `_teams_conv_refs` is in-memory here — a chat won't be reachable proactively until it has
> sent at least one activity since the last restart. For production, persist references to
> Supabase (there's already a Supabase client in `server.py`) keyed by conversation id.

---

## 7. Register the endpoint + deploy

1. **Azure Bot resource → Configuration → Messaging endpoint** =
   `https://<your-deployed-host>/api/messages`. Save.
2. Confirm **Teams channel** is enabled on the Azure Bot resource.
3. Deploy the backend (`deploy.ps1` ships the working tree per `CLAUDE.md`; or the AWS
   `buildspec.yml` / `Dockerfile` path). Ensure port 5000 is fronted by HTTPS.
4. Install/upload the MASE app into a test group chat (Teams → Apps → upload custom app),
   which triggers the RSC consent prompt you screenshotted.

---

## 8. Test matrix (maps to your requirements)

| Requirement | How to test | Expected |
|---|---|---|
| Added to a chat | Add MASE to a group chat | Greeting message appears (step 4 `conversationUpdate`) |
| Read past history | Say "summarize this chat" | MASE calls Graph, returns a summary of prior messages |
| **Trigger on every new message** | Post any message in the group | `/api/messages` receives a `message` activity |
| Manual trigger | `@MASE what's the status of <deal>` | Mention stripped, agent replies |
| Write messages | Any of the above | Reply posts back into the chat |
| Proactive push | `POST /api/teams/notify` with a known conversation id | Message appears unprompted |

---

## 9. Gotchas checklist

- [ ] App ID identical across manifest / Azure Bot / backend env.
- [ ] `MicrosoftAppType` + `MicrosoftAppTenantId` set for single-tenant, or JWT validation fails.
- [ ] Channel triggering needs `ChannelMessage.Read.Group` RSC; group chats don't.
- [ ] Bot **send** uses the Connector API (fine) — this is NOT the app-only *Graph* send
      limitation the connector files warn about. That warning only affects Graph POSTs.
- [ ] Reconcile the two existing Graph connectors: keep **`msteams_mcp_server.py`** (app-only,
      matches the bot's client-credentials model) for history; `teams_mcp_server.py` (delegated)
      is redundant for the bot and can be left dormant.
- [ ] Add a `CHANGELOG.md` entry when this ships (repo convention).
- [ ] The endpoint bypasses whatever auth guards `/api/chat` — Bot Framework JWT is the only
      gate. Don't leak other internal routes on the same public host without auth.
