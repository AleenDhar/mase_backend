# Enabling the Metered Teams Messages Graph API (for MASE bot history reading)

**Goal:** let the MASE Teams bot read a group chat / channel's **message history** (e.g.
"@MASE summarize this chat"). Reading history uses Microsoft Graph's Teams message-list
APIs, which Microsoft gates behind a **payment model** that a tenant admin must enable
before the app can call them. This is the one remaining IT action for the history feature.

> **Scope note:** this ONLY affects reading *historical* messages. Live messages (sent
> after the bot joins) already work for free via Bot Framework. So the bot is fully
> functional today; this unlocks the back-scroll / "summarize" capability only.

---

## Key identifiers

| | |
|---|---|
| Entra app (client) ID | `98489e0f-9826-430b-b396-a74cc3719d97` |
| Tenant ID | `e59a1851-bfcf-4536-8dd7-bd19f398e625` |
| Azure account (existing) | `022187637784` (AWS — for reference; Azure subscription is separate) |

---

## Which Graph APIs require this

- `GET /chats/{chat-id}/messages` — list a group chat's messages
- `GET /teams/{team-id}/channels/{channel-id}/messages` — list a channel's messages
- (and their `delta` / change-notification variants)

These are Microsoft's **"protected/metered" Teams messaging APIs**. Without a payment
model enabled, they return `403 Forbidden` with a "payment required / protected API" error.

---

## Which payment model — use **Model B (metered / pay-as-you-go)**

Microsoft offers two models:

| Model | Cost | Eligibility | Our fit |
|---|---|---|---|
| **A — Security & Compliance** | Free | Approved only for eDiscovery / DLP / backup-type apps, via a Microsoft request form | ❌ A sales assistant does **not** qualify |
| **B — Metered / pay-as-you-go** | Per-message fee, billed to an Azure subscription | Any app | ✅ **This is the one to enable** |

**Model B has a free monthly evaluation quota** (a "seeded" capacity per app, per tenant),
so we can pilot at **zero cost** and only pay if usage exceeds the free tier.

---

## Permissions — already handled (no new consent needed)

The bot reads messages via **RSC (Resource-Specific Consent)**: the manifest already
declares `ChatMessage.Read.Chat` and `ChannelMessage.Read.Group`, consented when a
group/team owner adds the bot. That authorizes reading the specific chat/team the bot is in
— **no tenant-wide application permission or extra admin consent is required.**

*(Optional, only if you later want tenant-wide reads beyond the bot's own chats: grant the
application permissions `Chat.Read.All` / `ChannelMessage.Read.All` with admin consent. Not
needed for the bot's own use.)*

So the enablement below is **billing/metering setup only**.

---

## Enablement steps (IT — in the Azure portal)

Follow Microsoft's doc **"Enable metered APIs and services in Microsoft Graph"**
(learn.microsoft.com) — summary:

1. **Azure subscription** — ensure an active Azure subscription exists in tenant
   `e59a1851-…`. Note its **Subscription ID**.

2. **Register the resource provider** — Azure portal → **Subscriptions** → *(that
   subscription)* → **Resource providers** → search **`Microsoft.GraphServices`** →
   **Register**. (Status must show *Registered*.)

3. **Confirm the app** — Entra ID → App registrations → app `98489e0f-…` exists and is the
   one the bot uses. (It does — this is the shared MASE app.) No new API permissions needed
   thanks to RSC (see above).

4. **Payment model on calls** — the app sends `?model=B` on each metered request. **This is
   a code change on our side, not yours** — we handle it behind a feature flag. Nothing for
   IT to configure here; it's noted so the billing you see maps to our calls.

5. **(Optional) set a budget/alert** — Azure portal → Cost Management → create a budget +
   alert on the subscription so metered Graph usage is visible and capped.

> Exact portal menu paths shift over time — the Microsoft doc above is the source of truth.
> The essentials: **Model B**, `Microsoft.GraphServices` **registered**, active
> **subscription**, RSC permissions already consented.

---

## Cost expectations

- **Evaluation mode:** a free monthly quota of message reads per app/tenant — enough for a
  pilot. See Microsoft's **"Payment models and licensing requirements for Microsoft Teams
  APIs"** for the current seeded capacity.
- **Beyond the free quota:** a small per-message fee (see Microsoft's current metered
  pricing). Our usage is on-demand (only when a user asks MASE to read history), and each
  request reads a bounded page of recent messages — so volume is low and controllable. We
  can cap it in code (max messages per read) and via the Azure budget in step 5.

---

## What we (dev) do once you confirm

1. Add the `model=B` parameter to the bot's Graph history calls.
2. Flip **`history_enabled`** ON in the MASE Teams control room (already wired; currently a
   no-op switch).
3. Test: "@MASE summarize this chat" pulls and summarizes the back-scroll.

---

## What to send back to us

Once done, please confirm:
- ✅ `Microsoft.GraphServices` **registered** on subscription **`<Subscription ID>`**
- ✅ Subscription is **active** in tenant `e59a1851-…`
- (Any Microsoft onboarding/approval reference, if applicable)

Then we enable the feature on our side. Happy to jump on a 10-minute call to do steps 1–2
together.

---

## References (Microsoft Learn — authoritative)

- *Payment models and licensing requirements for Microsoft Teams APIs*
- *Enable metered APIs and services in Microsoft Graph*
- *Microsoft Teams API — protected APIs (change notifications with resource data)*
