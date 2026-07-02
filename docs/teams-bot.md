# MASE Teams Bot — Full Context

Canonical reference for the MASE Microsoft Teams bot: what it is, how it's wired, how it
deploys, the bugs that were fixed to get it live, and what's still planned. For the
step-by-step first-time setup, see [teams-bot-setup.md](teams-bot-setup.md).

**Status: LIVE in production (since 2026-07-01).** 1:1 chat and group chat (@mention) both
work, with personalized greetings and per-conversation memory.

---

## 1. What it is

A **Bot Framework bot** that fronts the MASE sales-intelligence agent from inside Microsoft
Teams. Add MASE to a 1:1, group chat, or channel; message it (or @mention it in a group) and
it runs the same MASE agent that powers `/api/chat` and replies in the chat.

It is a Zycus-owned bot identity (not a personal account) and uses **RSC (Resource-Specific
Consent)** — when a group/team owner adds it, it gets access scoped to *that* chat/team.

### Push model vs. the old pull model
- **This bot = push model (Bot Framework).** Teams POSTs every relevant message to a
  messaging endpoint on the backend; the bot replies via the Bot Connector API. This is the
  only model that supports "trigger on new message" + posting back.
- **`msteams_mcp_server.py` / `teams_mcp_server.py` = pull model (Graph read connectors).**
  Optional, used only for *reading* history. The earlier delegated-token "Teams MCP" was
  **decommissioned** (wrong model) — this bot is its replacement.

---

## 2. Request flow

```
User in Teams (1:1 / group / channel)
   │  message, or @MASE mention
   ▼
Azure Bot "mase-bot"  ──POST──►  https://mase-api.zycus.com/api/messages
                                        │  Authorization: Bot Framework JWT
                                        ▼
                            teams_bot.py  CloudAdapter.process_activity
                                        │  (validates JWT; runs on_turn)
                                        ├─ conversationUpdate + bot added → greet
                                        └─ message:
                                             1. mention gate (group/channel → require @MASE)
                                             2. send immediate ACK  (POST returns < ~15s)
                                             3. background task:
                                                  _teams_agent_reply(text, conv_id, user_name)
                                                    → server.py → agent_manager.get_agent()
                                                    → agent.ainvoke(configurable.thread_id=chat_id)
                                             4. proactive post the answer via
                                                adapter.continue_conversation(ref)
```

**Why async (ack → background → proactive):** MASE agent runs take 30s–minutes, but the Bot
Framework activity POST must return within ~15s or Teams times out. So the bot acks
immediately, runs the agent in the background, and posts the real answer proactively using
the stored conversation reference.

---

## 3. Identifiers & credentials

| Thing | Value |
|---|---|
| Azure Bot resource | **`mase-bot`** (single-tenant) |
| Microsoft App (client) ID | **`98489e0f-9826-430b-b396-a74cc3719d97`** |
| Tenant ID | **`e59a1851-bfcf-4536-8dd7-bd19f398e625`** |
| Teams app package | `mase-teams-app.zip`, Teams app id **`0dd518d6-9200-4583-9dd4-c020a6860351`** |
| Messaging endpoint | **`https://mase-api.zycus.com/api/messages`** |
| RSC permissions | `ChannelMessage.Read.Group`, `ChatMessage.Read.Chat`, `TeamMember.Read.Group`, `ChatMember.Read.Chat` |

> ⚠️ **The App ID `98489e0f…` is shared** — it backs SSO **+** the Teams bot **+** Outlook.
> Guard its secret; do **not** change its redirect URIs or public-client settings.

**Where the secret lives:** AWS Secrets Manager **`mase/app-env`**, keys `MS_CLIENT_ID` /
`MS_CLIENT_SECRET` / `MS_TENANT_ID`, injected into the ECS tasks as container secrets. No
separate bot secret was ever needed — the bot reuses the shared Entra app's creds.

---

## 4. Infrastructure

| Component | Value |
|---|---|
| AWS account / region | `022187637784` / `ap-south-1` |
| AWS CLI profile | `backend` |
| Public host | `https://mase-api.zycus.com` (Akamai CNAMEs → ALB; ACM cert issued) |
| ALB | `mase-alb` |
| HTTP:80 listener | `…/c6710f58972ca338` |
| HTTPS:443 listener | `…/900dc459aa97463c` |
| Target groups | `mase-blue/71c71534374ec831`, `mase-green/c8b1ab1c4dff2dbf` |
| ECS cluster | `mase-cluster` (services `mase-api-blue`, `mase-api-green`, plus `mase-worker`) |
| ECR repo | `022187637784.dkr.ecr.ap-south-1.amazonaws.com/mase-service` |
| IT / DNS contact | Hitesh Mahajan |

---

## 5. Code map

| File | Role |
|---|---|
| `teams_bot.py` | The whole bot: `/api/messages` route, adapter, turn handler, proactive reply, mention gate, config. |
| `server.py` | `_teams_agent_reply` bridge + route registration; `/api/messages` in the auth allowlist. |
| `requirements.txt` | `botbuilder-core`, `botbuilder-integration-aiohttp`. |
| `.github/workflows/deploy.yml` | Blue/green deploy; flips **both** HTTP + HTTPS listeners. |
| `docs/teams-bot-setup.md` | First-time setup runbook. |

### Key functions in `teams_bot.py`
- **`_cfg()`** — reads creds, preferring `MS_CLIENT_ID/MS_CLIENT_SECRET/MS_TENANT_ID`, then
  `BOT_APP_*`, then `MicrosoftApp*`. `BOT_APP_TYPE` defaults to `SingleTenant`.
- **`_BotConfig`** — an **object** exposing `APP_ID / APP_PASSWORD / APP_TYPE / APP_TENANTID`.
  botbuilder reads these via `getattr`, so it MUST be an object, not a dict (see Gotchas).
- **`_build_adapter()`** — builds the `CloudAdapter` from `ConfigurationBotFrameworkAuthentication(_BotConfig(...))`.
- **`_mentioned_ids(activity)`** — IDs @mentioned in a message (reads `.mentioned` or raw
  `additional_properties`), used by the mention gate.
- **`on_turn(turn)`** — greets on install; on a message applies the mention gate, sends the
  ack, and spawns the background agent run.
- **`_run_and_post(ref, text, conv_id, user_name)`** — runs the agent, posts the answer
  proactively via `continue_conversation`.
- **`register_teams_bot(app, agent_reply)`** — mounts `POST /api/messages`.

### Key wiring in `server.py`
- **`_teams_agent_reply(user_text, conversation_id, user_name)`** — bridges to the existing
  agent: `agent_manager.get_agent()` → `ainvoke({"messages":[…]}, config={"recursion_limit":…,
  "configurable":{"thread_id": f"teams:{conversation_id}"}})`. Prepends `[Teams user: <name>]`
  so the agent greets the real user. Flattens Anthropic list-content to text.
- Registered on `_fastapi_app` **before** it's wrapped by `_AppWithMCP`.
- `/api/messages` is in **`_API_AUTH_PUBLIC_EXACT`** (auth handled internally by the Bot
  Framework JWT, not the MASE API token).

---

## 6. Behavior rules

- **1:1 ("personal") chat** → responds to **every** message.
- **Group chat / channel** → responds **only when @MASE is mentioned** (Teams delivers all
  group messages to the bot, so without this gate it would reply to everything).
- **Personalized** — greets the sender by their Teams display name (`from_property.name`).
- **Per-conversation memory** — `thread_id = teams:<conversation_id>`, so each chat keeps its
  own agent memory thread.
- **On install** — posts a short greeting proving the install + RSC consent path.

---

## 7. Deployment

- **Trigger:** push to `main` (or manual `workflow_dispatch`). Pushes to other branches do
  nothing; main pushes touching only `docs/**`, `*.md`, `.agents/**` are skipped.
- **`deploy.ps1` is deprecated — do not use it.** Deploy is GitHub Actions only.
- **Pipeline:** OIDC assume AWS role → pull real `mcp_config.json` from Secrets Manager
  `mase/mcp-config` (build fails if Salesforce/Avoma missing) → build & push image to ECR →
  register task defs (`.github/deploy/render_taskdef.py`) → **blue/green**: deploy to idle
  colour, wait healthy, **flip both HTTP + HTTPS listeners**, smoke-test + selfcheck gate
  with **auto-rollback**, drain old colour. Also rolls the `mase-worker` service.
- **Flow to ship a change:** branch → PR → merge to `main` → Actions deploys.

### Known deploy flake
The smoke-test gate hits `/api/deal-engine/selfcheck` ~10s after the flip. On a cold start,
MCP servers may still be background-loading, so selfcheck returns not-ok → gate fails →
auto-rollback (to the previous, working image). **Fix: re-run the workflow** ("Re-run all
jobs"); it usually passes on the second attempt. If it flakes repeatedly, bump the smoke
test's `sleep`. This is pipeline timing, unrelated to the bot code.

---

## 8. Observability / debugging

- **Logs:** CloudWatch log group **`/ecs/mase-service`**, filter for `[TEAMS BOT]`.
  - `adapter: app_id=set type=SingleTenant tenant=set secret=set` — startup cred check.
  - `activity type=message conv=… user='…' text='…'` — inbound message.
  - `proactive reply posted conv=… (N chars)` — answer sent.
  - `agent_reply failed …` / `proactive post failed …` — errors.
- ⚠️ On **Git Bash**, prefix AWS log commands with `MSYS_NO_PATHCONV=1` or the
  `/ecs/mase-service` arg gets mangled into a Windows path.
- **Endpoint smoke check:** `GET /api/messages` should return **405** (route exists,
  POST-only, gate passing). `401` = auth gate blocking; `404` = not deployed.

---

## 9. Build history (bugs fixed to get it live)

| PR | Change | Why it was needed |
|---|---|---|
| #2 | Endpoint + `deploy.yml` HTTPS-listener flip | HTTPS listener would point at the drained colour after each deploy, killing the endpoint. |
| #3 | `/api/messages` → auth allowlist | Global API-auth gate 401'd Teams (its JWT ≠ `API_AUTH_TOKEN`). |
| #4 | Pass a config **object**, not a dict | botbuilder read creds via `getattr(APP_ID…)`; a dict → null app id → "Invalid AppId passed on token". |
| #5 | `configurable.thread_id` on `ainvoke` | The agent's checkpointer requires a `thread_id`. |
| #6 | Pass the Teams sender's name | Agent had no identity, so it hallucinated names ("Hi Krina!"). |
| #7 | Mention-gating in group/channel | Bot was replying to *every* group message, not just when addressed. |

Each fix uncovered the next layer; the reply path is now proven end-to-end.

---

## 10. Current status

✅ Live in prod. 1:1 works (ack → answer, greets by name). Group chat works (silent unless
@mentioned). Per-conversation memory. HTTPS survives deploys. Full `[TEAMS BOT]` logging.

---

## 11. Pending & planned

### A. Read past group history — ⏳ blocked on IT, code not yet built
Reading messages sent *before* the bot joined uses Graph `GET /chats/{id}/messages`, which
Microsoft gates behind the **metered Teams messages Graph API (Model B)**. IT must enable it
(register `Microsoft.GraphServices`, admin-consent `ChatMessage.Read.All` /
`ChannelMessage.Read.All`, link an Azure subscription; free evaluation quota to pilot). The
RSC permission is already consented; only billing + the fetch code (behind
`TEAMS_HISTORY_ENABLED`) remain. *Live messages after join are free via Bot Framework and
already work.*

### B. User-based access (allowlist) — planned, not yet built
Only people on an allowed list may use the bot; others get a polite "not authorized" reply.
Decision: the allowlist is **managed in the control room** (backed by Supabase so it persists
and the bot reads it per message). Sender identity resolution likely via
`TeamsInfo.get_member()` (email/UPN) since the raw activity only carries display name + Teams
user id.

### C. Control room UI panel inside MASE — planned, not yet built
An admin page (e.g. `/api/teams/admin`, behind MASE's existing `?key=`/cookie auth) with:
allowlist management, recent activity log, and a history toggle + metered-API status
indicator. Follows the existing HTML-admin-panel pattern (see the deal-engine dashboards) and
the httpx-REST Supabase store pattern (`analysis_store.py`).

---

## 12. Invariants — do not break these

1. **`deploy.yml` must flip BOTH listeners** (HTTP `c6710f58…` + HTTPS `900dc459…`).
2. **`/api/messages` must stay in `_API_AUTH_PUBLIC_EXACT`** — its auth is the Bot Framework
   JWT, not the MASE token.
3. **botbuilder auth needs a config OBJECT** (`APP_ID/APP_PASSWORD/APP_TYPE/APP_TENANTID`),
   never a dict.
4. **Agent `ainvoke` needs `configurable.thread_id`** (checkpointer requirement).
5. **Single-tenant** — `APP_TYPE=SingleTenant` + a valid tenant id, or JWT validation fails.
6. Keep the **async ack → proactive reply** shape — synchronous replies time out.
