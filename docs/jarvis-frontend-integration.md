# Jarvis frontend integration guide

How to build the **Jarvis** UI: a single cross-analysis chat assistant plus a
**settings tab** where the user picks which analyses Jarvis can read and edits its
system prompt. Backend is already built — this is the contract the frontend codes
against. For the underlying analysis tables/realtime model see
[frontend-analysis-integration.md](frontend-analysis-integration.md); for the
backend internals see [analysis-feature.md](analysis-feature.md).

## What Jarvis is (mental model)

- **One global agent, many analyses.** Unlike the per-analysis chat, Jarvis reads
  across a GLOBAL set of "enabled" analyses at once. The enabled set + the system
  prompt are a single global singleton (`jarvis_settings`, one row) — there is no
  per-user or per-project scoping. Changing them changes Jarvis for everyone.
- **Search analyses first, native tools as fallback.** Jarvis always searches the
  enabled analyses first (via its `jarvis_*` tools) and only falls back to the
  shared read-only native catalog (Salesforce/CRM reads, web search, other MCP
  tools) when the answer isn't in the analyses. It is strictly read-only — it never
  writes to Salesforce or any external system. This routing is enforced server-side
  and cannot be turned off from the UI.

## UI requirements

The Jarvis surface is two areas: a **chat panel** and a **settings tab**. These are
the requirements the frontend must satisfy.

### Settings tab

- **Analyses checklist.** Render every analysis from `GET /api/jarvis/settings`
  `analyses[]` as a toggle/checkbox list. Pre-check the ones with `enabled: true`.
  Show each analysis's `title` and `status` badge (`draft` / `running` / `done` /
  `error`); a `done` analysis is the most useful one to enable. If `analyses` is
  empty, show an empty state ("No analyses yet — create one first").
- **System-prompt editor.** A multi-line textarea bound to `system_prompt`.
  - When `system_prompt` is `""`, leave the textarea empty and show
    `default_system_prompt` as muted placeholder text so the user sees what Jarvis
    runs with by default.
  - Provide a **"Reset to default"** control that clears the field and saves
    `system_prompt: ""`.
  - Make clear (helper text) that this is the persona only — Jarvis always also
    searches the enabled analyses first and stays read-only; the user does not need
    to list the analyses in the prompt.
- **Saving.** The toggles and the prompt save **independently** — send only the
  changed field to `PUT` (see below). Either auto-save on change (debounced) or
  give explicit Save buttons; either way reflect the returned values as the new
  source of truth (the backend de-dupes/validates ids and may drop invalid ones).
- **Global-scope warning.** Because settings are a single global singleton, changing
  them affects Jarvis for everyone. Surface a short note to that effect.
- **Feedback.** Show success/error toasts on save; on a non-200 from `PUT`, show the
  `error` string and keep the user's edits (don't silently revert).

### Chat panel

- **Threaded chat UI** posting to `POST /api/jarvis/chat/async`. Generate and keep
  a `chat_id` per conversation so you can subscribe to its events.
- **Incremental rendering** from Supabase realtime on `chat_messages` (ordered by
  `sequence`): stream `token` text into the assistant bubble; surface `tool_call` /
  `tool_result` / `thinking` as collapsible activity/"thinking" indicators;
  `status` as transient status text.
- **Terminal handling.** Stop the spinner only on the terminal row — `type=final`
  (success) or `type=error` (show the error). `status`/`ping` are not terminal.
- **Empty / disabled state.** If no analyses are enabled, Jarvis will reply saying
  so; detect zero enabled analyses up front and show a hint with a link/CTA to the
  settings tab.
- **Citations.** Jarvis cites analyses by title in its answers — render that text as
  given (no special parsing required).
- **Concurrency.** A `503` from the chat endpoint means the server is at capacity;
  show a "try again shortly" message rather than failing silently.

## Auth

All Jarvis routes are normal API routes (not `/mcp`), so they require the standard
bearer token on **every** request:

```
Authorization: Bearer <API token>
```

(Same token as `/api/analysis/*` and `/api/chat/*`.) Supabase realtime/reads from
the browser use the Supabase anon key as elsewhere.

## Settings tab

### GET `/api/jarvis/settings`

Returns everything the settings tab needs in one call: the saved enabled-ids, the
saved system prompt, the backend default prompt (use as the textarea placeholder /
"Reset to default" value), and EVERY analysis flagged with its current `enabled`
state so you can render the toggle list directly.

```jsonc
{
  "enabled_analysis_ids": ["4f610ffd-…", "aadaf402-…"],
  "system_prompt": "",                       // "" => Jarvis uses default_system_prompt
  "default_system_prompt": "You are Jarvis, a cross-analysis research assistant. …",
  "count": 3,                                 // number of analyses in `analyses`
  "analyses": [
    {
      "id": "4f610ffd-…",
      "title": "anthony greg",
      "status": "done",                       // draft | running | done | error
      "project_id": null,
      "updated_at": "2026-06-02T22:43:36Z",
      "enabled": true                         // is this analysis in the enabled set?
    }
    // …
  ]
}
```

Render `analyses` as a checklist; the checked items are the ones with
`enabled: true`. Show `system_prompt` in an editable textarea — if it's `""`, show
`default_system_prompt` as a muted placeholder so the user sees what Jarvis runs
with by default.

### PUT `/api/jarvis/settings`

Partial update — send only the field(s) you're changing. The other field is left
untouched, so the toggles and the prompt can be saved independently.

```jsonc
// Save the analysis toggles (the full enabled set; this REPLACES the list):
{ "enabled_analysis_ids": ["4f610ffd-…", "aadaf402-…"] }

// Save the system prompt:
{ "system_prompt": "You are Jarvis for the RevOps team. Lead with deal risk." }

// Reset the system prompt back to the backend default:
{ "system_prompt": "" }

// Both at once is fine too:
{ "enabled_analysis_ids": ["4f610ffd-…"], "system_prompt": "…" }
```

Response:

```jsonc
{
  "enabled_analysis_ids": ["4f610ffd-…"],     // cleaned + de-duped (invalid UUIDs dropped)
  "system_prompt": "You are Jarvis for the RevOps team. Lead with deal risk.",
  "count": 1
}
```

Notes:
- `enabled_analysis_ids` is a **full replacement** of the enabled set, not a delta.
  Send the complete list of checked analyses.
- IDs are UUID-validated and de-duped server-side; non-UUID entries are silently
  dropped, so trust the returned list over what you sent.
- Only the editable persona is stored. The live scope listing (which analyses,
  with titles) and the "search-analyses-first / read-only" operating rules are
  always appended by the backend at chat time — the user cannot edit those away,
  and they don't need to list the analyses in the prompt themselves.

### Realtime (optional)

`jarvis_settings` has realtime enabled (anon `SELECT` granted), so you can subscribe
to the single `id = 'global'` row to live-update the settings tab across tabs/users.
Writes are service-role only (go through PUT above) — the browser cannot write
directly.

## Chat

### POST `/api/jarvis/chat/async`

Mirrors `/api/chat/async` exactly — same request body, same transport, and the same
Supabase-persistence model. The only difference is the system prompt: the backend
composes it from the saved Jarvis settings (system prompt + enabled-analyses scope
+ operating rules). You do NOT pass the analyses or the prompt here.

Request body (`ChatRequest`):

```jsonc
{
  "messages": [ { "role": "user", "content": "Which open opps mention Snowflake?" } ],
  "chat_id": "…",            // optional; generated if omitted — KEEP it to subscribe
  "model": "openai:gpt-4o-mini",  // optional; provider:model
  "system_prompt": "…",      // optional EXTRA per-message instructions, appended after
                             //   the saved settings prompt (not a replacement)
  "headless": true
}
```

**Response transport (SSE keepalive).** The endpoint returns an
`text/event-stream` (SSE), NOT a JSON ack. It immediately emits a `chat_id` event,
then keeps the connection open — emitting `ping` events every ~5s while the agent
runs — and finishes with a `done` event (or an `error` event if the run itself
fails to start). The agent's actual output is NOT in this stream; it is persisted
to Supabase as it's produced.

```
data: {"type":"chat_id","chat_id":"…"}
data: {"type":"ping"}            // periodic keepalive, ignore
data: {"type":"done","chat_id":"…"}   // or {"type":"error","content":"…"}
```

So you have two equivalent options:
- **Recommended:** read the `chat_id` (from the body you sent or the first SSE
  event), then ignore the stream and render everything from Supabase realtime
  (below). The SSE stream exists mainly to hold the connection open / signal start
  and end.
- Or consume the SSE stream just for lifecycle (`chat_id` → start, `done`/`error`
  → finished) while still rendering content from Supabase realtime.

### Reading the answer (Supabase realtime on `chat_messages`)

Subscribe to `chat_messages` filtered by your `chat_id`, ordered by `sequence`.
Each row is one event:

| column     | meaning |
|------------|---------|
| `chat_id`  | your chat id (UUID) |
| `role`     | `assistant` |
| `type`     | event kind: `status`, `tool_call`, `thinking`, `tool_result`, `token`, `final`, `error` |
| `content`  | the text/payload for that event |
| `sequence` | monotonic per-chat ordering — sort by this |
| `metadata` | JSON string with extra context (when present) |

Render incrementally: stream `token` rows as they arrive, show `tool_call` /
`tool_result` / `thinking` as activity. **Terminal contract:** exactly ONE
terminal row ends the turn — `type = "final"` (success) OR `type = "error"`. Stop
your spinner on either. `status` is NOT terminal.

## Quick build checklist

1. Settings tab: `GET /api/jarvis/settings` → render the analyses checklist
   (`enabled` flags) + a system-prompt textarea (placeholder = `default_system_prompt`).
2. Save toggles → `PUT { enabled_analysis_ids: [...] }`; save prompt →
   `PUT { system_prompt: "..." }`; "Reset to default" → `PUT { system_prompt: "" }`.
3. Chat: `POST /api/jarvis/chat/async` with `messages` + a `chat_id` you keep.
4. Subscribe to `chat_messages` (filter `chat_id`, order `sequence`); render events;
   finish on `final`/`error`.
5. Empty state: if no analyses are enabled, Jarvis will say so — surface a hint
   linking to the settings tab.
