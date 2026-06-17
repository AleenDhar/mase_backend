# CHANGELOG — MASE backend (`mase_backend`)

> **Agents & teammates: read this file after every `git pull`.** It is the running
> log of behaviour-changing decisions and conventions. Newest first. When you make a
> change that affects how the system behaves, where data lives, or how another agent
> should work, **add an entry here** (and, for a durable rule, a note under
> `.agents/memory/` with a line in `.agents/memory/MEMORY.md`).

Conventions for an entry: `## YYYY-MM-DD — <short title>`, then **What / Why /
How to work with it going forward**. Keep it tight; link code paths and docs.

---

## 2026-06-18 — System prompts now live in Supabase (Supabase is the SOURCE OF TRUTH)

**What.** The two MASE agent system prompts are now stored in, and served from,
Supabase — not the local `prompts/*.md` files:

| Agent | Supabase row (`public.jarvis_settings.id`) | Edit it from |
| --- | --- | --- |
| Deal Intelligence Engine **sweep** | `mase_deal_sweep` | Admin → Agent Control → **Deal Sweep** (or `POST /api/deal-engine/sweep/prompt`) |
| **Todo Runner** ("Run with AI" Tactical Fulfillment) | `mase_todo_runner` | Admin → Agent Control → **Todo Runner** (or `POST /api/deal-engine/todo-runner/prompt`) |

Both rows are seeded with the current prompt text and read at runtime via
`agent_prompt_store.get_prompt(<id>)`. The chat agent key `mase_chat_agent` already
worked this way.

**Why.** So the prompts can be edited live by admins without a code change/redeploy,
and so there is ONE authoritative copy. The deal-sweep agent re-resolves the prompt
on a 15s TTL and rebuilds when its fingerprint changes (`deal_engine_sweep._get_agent`);
the todo-runner fetches it per run from the frontend (`components/agent/AgentRun.tsx`).

**How to work with it going forward.**
- ✅ To change an agent's behaviour, **edit the Supabase prompt** (via the Admin UI or
  the endpoint above). Supabase ALWAYS wins.
- ⚠️ Do **NOT** edit `prompts/deal_engine_sweep_system_prompt.md` or
  `prompts/todo_runner_system_prompt.md` to change live behaviour. They are now only
  the **cold-start SEED / fallback** (used only if the Supabase row is missing) and
  carry a `⚠️ DEPRECATED` banner at the top. That banner is a leading HTML comment
  stripped at load (`agent_prompt_store.strip_leading_banner`) so it never enters the
  prompt. If you intentionally improve the seed, mirror the change into Supabase too.
- The Admin editor's **"Reset to default"** clears the Supabase override and falls
  back to the seed — that's the only path back to the on-disk version.
- See `.agents/memory/prompts-source-of-truth.md`.

## 2026-06-18 — Admin → Execution shows two separate run feeds

**What.** The Admin → Execution tab now lists **Deal Sweep runs** (worker status +
`/api/deal-engine/trigger-logs`) and **Todo Runner runs** separately. The latter is a
new endpoint `GET /api/deal-engine/todo-runner/runs` that identifies "Run with AI"
runs by their seed user-message in the shared `chats`/`chat_messages` tables (no
schema change) and derives each run's status (draft_ready / needs_human / error /
running). Admin-gated at the Next.js proxy.

## 2026-06-18 — Agent doc upload hardened

**What.** `POST /api/documents/upload` (Admin → Knowledge) accepts PDF/DOCX (`file_b64`
+ `filename`) and `doc_type`; extraction runs off the event loop with a 120s timeout
and is bounded (size/pages/chars). Endpoint is no longer in the public allowlist.
